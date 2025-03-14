import abc
import enum
import os
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

import requests
from kubernetes import client, config, watch

from vllm_router.log import init_logger

logger = init_logger(__name__)

_global_service_discovery: "Optional[ServiceDiscovery]" = None


class ServiceDiscoveryType(enum.Enum):
    STATIC = "static"
    K8S = "k8s"
    COMBINED = "combined"
    EXTERNAL = "external"


@dataclass
class EndpointInfo:
    # Endpoint's url
    url: str

    # Model name
    model_names: List[str]

    # Added timestamp
    added_timestamp: float


class ServiceDiscovery(metaclass=abc.ABCMeta):
    @abc.abstractmethod
    def get_endpoint_info(self) -> List[EndpointInfo]:
        """
        Get the URLs of the serving engines that are available for
        querying.

        Returns:
            a list of engine URLs
        """
        pass

    def get_health(self) -> bool:
        """
        Check if the service discovery module is healthy.

        Returns:
            True if the service discovery module is healthy, False otherwise
        """
        return True

    def close(self) -> None:
        """
        Close the service discovery module.
        """
        pass


class StaticServiceDiscovery(ServiceDiscovery):
    def __init__(self, urls: List[str], models: List[str]):
        assert len(urls) == len(models), "URLs and models should have the same length"
        self.urls = urls
        self.models = models
        self.added_timestamp = int(time.time())

    def get_endpoint_info(self) -> List[EndpointInfo]:
        """
        Get the URLs of the serving engines that are available for
        querying.

        Returns:
            a list of engine URLs
        """
        return [
            EndpointInfo(url, models, self.added_timestamp)
            for url, models in zip(self.urls, self.models)
        ]


class K8sServiceDiscovery(ServiceDiscovery):
    def __init__(self, namespace: str, port: str, label_selector=None):
        """
        Initialize the Kubernetes service discovery module. This module
        assumes all serving engine pods are in the same namespace, listening
        on the same port, and have the same label selector.

        It will start a daemon thread to watch the engine pods and update
        the url of the available engines.

        Args:
            namespace: the namespace of the engine pods
            port: the port of the engines
            label_selector: the label selector of the engines
        """
        self.namespace = namespace
        self.port = port
        self.available_engines: Dict[str, EndpointInfo] = {}
        self.available_engines_lock = threading.Lock()
        self.label_selector = label_selector

        # Init kubernetes watcher
        try:
            config.load_incluster_config()
        except:
            config.load_kube_config()

        self.k8s_api = client.CoreV1Api()
        self.k8s_watcher = watch.Watch()

        # Start watching engines
        self.running = True
        self.watcher_thread = threading.Thread(target=self._watch_engines, daemon=True)
        self.watcher_thread.start()

    @staticmethod
    def _check_pod_ready(container_statuses):
        """
        Check if all containers in the pod are ready by reading the
        k8s container statuses.
        """
        if not container_statuses:
            return False
        ready_count = sum(1 for status in container_statuses if status.ready)
        return ready_count == len(container_statuses)

    def _get_model_names(self, pod_ip) -> Optional[List[str]]:
        """
        Get the model names of the serving engine pod by querying the pod's
        '/v1/models' endpoint.

        Args:
            pod_ip: the IP address of the pod

        Returns:
            the model names of the serving engine
        """
        url = f"http://{pod_ip}:{self.port}/v1/models"
        try:
            headers = None
            if VLLM_API_KEY := os.getenv("VLLM_API_KEY"):
                logger.info(f"Using vllm server authentication")
                headers = {"Authorization": f"Bearer {VLLM_API_KEY}"}
            response = requests.get(url, headers=headers)
            response.raise_for_status()

            response_json = response.json()
            if "data" not in response_json:
                return None

            model_names = [model["id"] for model in response_json["data"]]
            if len(model_names) == 0:
                return None

            return model_names
        except Exception as e:
            logger.error(f"Failed to get model name from {url}: {e}")
            return None

    def _watch_engines(self):
        # TODO (ApostaC): remove the hard-coded timeouts

        while self.running:
            try:
                for event in self.k8s_watcher.stream(
                    self.k8s_api.list_namespaced_pod,
                    namespace=self.namespace,
                    label_selector=self.label_selector,
                    timeout_seconds=30,
                ):
                    pod = event["object"]
                    event_type = event["type"]
                    pod_name = pod.metadata.name
                    pod_ip = pod.status.pod_ip
                    is_pod_ready = self._check_pod_ready(pod.status.container_statuses)
                    if is_pod_ready:
                        model_names = self._get_model_names(pod_ip)
                    else:
                        model_names = None
                    self._on_engine_update(
                        pod_name, pod_ip, event_type, is_pod_ready, model_names
                    )
            except Exception as e:
                logger.error(f"K8s watcher error: {e}")
                time.sleep(0.5)

    def _add_engine(self, engine_name: str, engine_ip: str, model_names: List[str]):
        logger.info(
            f"Discovered new serving engine {engine_name} at "
            f"{engine_ip}, running models: {model_names}"
        )
        with self.available_engines_lock:
            self.available_engines[engine_name] = EndpointInfo(
                url=f"http://{engine_ip}:{self.port}",
                model_names=model_names,
                added_timestamp=int(time.time()),
            )

    def _delete_engine(self, engine_name: str):
        logger.info(f"Serving engine {engine_name} is deleted")
        with self.available_engines_lock:
            del self.available_engines[engine_name]

    def _on_engine_update(
        self,
        engine_name: str,
        engine_ip: Optional[str],
        event: str,
        is_pod_ready: bool,
        model_names: Optional[List[str]],
    ) -> None:
        if event == "ADDED":
            if engine_ip is None:
                return

            if not is_pod_ready:
                return

            if model_names is None:
                return

            self._add_engine(engine_name, engine_ip, model_names)

        elif event == "DELETED":
            if engine_name not in self.available_engines:
                return

            self._delete_engine(engine_name)

        elif event == "MODIFIED":
            if engine_ip is None:
                return

            if (
                not is_pod_ready or model_names is None
            ) and engine_name in self.available_engines:
                self._delete_engine(engine_name)
                return

            self._add_engine(engine_name, engine_ip, model_names)

    def get_endpoint_info(self) -> List[EndpointInfo]:
        """
        Get the URLs of the serving engines that are available for
        querying.

        Returns:
            a list of engine URLs
        """
        with self.available_engines_lock:
            return list(self.available_engines.values())

    def get_health(self) -> bool:
        """
        Check if the service discovery module is healthy.

        Returns:
            True if the service discovery module is healthy, False otherwise
        """
        return self.watcher_thread.is_alive()

    def close(self):
        """
        Close the service discovery module.
        """
        self.running = False
        self.k8s_watcher.stop()
        self.watcher_thread.join()


class CombinedServiceDiscovery(ServiceDiscovery):
    def __init__(self, *service_discoveries: ServiceDiscovery):
        """
        Initialize a combined service discovery module that merges results from
        multiple discovery mechanisms.

        Args:
            *service_discoveries: Variable number of ServiceDiscovery instances
        """
        self.service_discoveries = service_discoveries

        # Validate that we have at least one service discovery
        if not service_discoveries:
            raise ValueError("At least one service discovery module must be provided")

    def get_endpoint_info(self) -> List[EndpointInfo]:
        """
        Get the combined URLs from all service discovery mechanisms.

        Returns:
            a combined list of EndpointInfo objects from all sources
        """
        all_endpoints = []
        for sd in self.service_discoveries:
            all_endpoints.extend(sd.get_endpoint_info())
        return all_endpoints

    def get_health(self) -> bool:
        """
        Check if all service discovery modules are healthy.

        Returns:
            True if all service discovery modules are healthy, False otherwise
        """
        return all(sd.get_health() for sd in self.service_discoveries)

    def close(self) -> None:
        """
        Close all service discovery modules.
        """
        for sd in self.service_discoveries:
            sd.close()


class ExternalServiceDiscovery(ServiceDiscovery):
    def __init__(self, urls: List[str]):
        """
        Initialize the External service discovery module. This module
        automatically discovers models from OpenAI-compatible endpoints
        by querying their /v1/models endpoint.

        Args:
            urls: List of URLs of external OpenAI-compatible backends
        """
        self.urls = urls
        self.endpoint_infos: List[EndpointInfo] = []
        self.added_timestamp = int(time.time())

        # Discover models for all endpoints
        self._discover_models()

    def _discover_models(self):
        """
        Discover models for all external endpoints by querying
        their /v1/models endpoint.
        """
        self.endpoint_infos = []
        for url in self.urls:
            discovered_models = self._get_model_names(url)
            if discovered_models:
                self.endpoint_infos.append(
                    EndpointInfo(
                        url=url,
                        model_names=discovered_models,
                        added_timestamp=self.added_timestamp
                    )
                )
                logger.info(f"Discovered models for external endpoint {url}: {discovered_models}")
            else:
                logger.warning(f"Failed to discover models for external endpoint {url}, skipping")

    def _get_model_names(self, backend_url: str) -> Optional[List[str]]:
        """
        Get the model names of the external endpoint by querying the
        '/v1/models' endpoint.

        Args:
            backend_url: the URL of the backend

        Returns:
            the model names of the external endpoint, or None if failed
        """
        url = f"{backend_url}/v1/models"
        try:
            headers = None
            if VLLM_API_KEY := os.getenv("VLLM_API_KEY"):
                logger.info(f"Using vllm server authentication")
                headers = {"Authorization": f"Bearer {VLLM_API_KEY}"}
            response = requests.get(url, headers=headers, timeout=5)
            response.raise_for_status()

            response_json = response.json()
            if "data" not in response_json:
                return None

            model_names = [model["id"] for model in response_json["data"]]
            if len(model_names) == 0:
                return None

            return model_names
        except Exception as e:
            logger.error(f"Failed to get model names from {url}: {e}")
            return None

    def get_endpoint_info(self) -> List[EndpointInfo]:
        """
        Get the URLs and models of the external endpoints.

        Returns:
            a list of EndpointInfo objects
        """
        return self.endpoint_infos


def _create_service_discovery(
    service_discovery_type: ServiceDiscoveryType, *args, **kwargs
) -> ServiceDiscovery:
    """
    Create a service discovery module with the given type and arguments.

    Args:
        service_discovery_type: the type of service discovery module
        *args: positional arguments for the service discovery module
        **kwargs: keyword arguments for the service discovery module

    Returns:
        the created service discovery module
    """

    if service_discovery_type == ServiceDiscoveryType.STATIC:
        return StaticServiceDiscovery(*args, **kwargs)
    elif service_discovery_type == ServiceDiscoveryType.K8S:
        return K8sServiceDiscovery(*args, **kwargs)
    elif service_discovery_type == ServiceDiscoveryType.EXTERNAL:
        return ExternalServiceDiscovery(*args, **kwargs)
    elif service_discovery_type == ServiceDiscoveryType.COMBINED:
        # Create component parts based on what's available
        service_discoveries = []

        # Process static service discovery part if URLs are provided
        static_urls = kwargs.get("static_urls", [])
        # Handle empty string or None
        if static_urls is None or (isinstance(static_urls, list) and len(static_urls) == 0) or static_urls == "":
            static_urls = []

        # If we have static URLs, create static service discovery
        if static_urls:
            static_models = kwargs.get("static_models", [])
            # Handle empty string or None
            if static_models is None or (isinstance(static_models, list) and len(static_models) == 0) or static_models == "":
                # Skip static service discovery if models are not provided
                logger.warning("Static backends provided but no models specified, skipping static discovery")
            else:
                # Verify that static_urls and static_models have the same length
                if len(static_urls) != len(static_models):
                    logger.warning(f"Static URLs and models have different lengths ({len(static_urls)} vs {len(static_models)}), skipping static discovery")
                else:
                    service_discoveries.append(StaticServiceDiscovery(static_urls, static_models))

        # Process K8s service discovery part if namespace and port are provided
        k8s_namespace = kwargs.get("k8s_namespace")
        k8s_port = kwargs.get("k8s_port")
        k8s_label_selector = kwargs.get("k8s_label_selector", "")

        # Skip k8s discovery if namespace is empty or None
        if k8s_namespace and k8s_namespace != "" and k8s_port:
            service_discoveries.append(K8sServiceDiscovery(k8s_namespace, k8s_port, k8s_label_selector))

        # Process external service discovery part if URLs are provided
        external_urls = kwargs.get("external_urls", [])
        # Handle empty string or None
        if external_urls is None or (isinstance(external_urls, list) and len(external_urls) == 0) or external_urls == "":
            external_urls = []

        # If we have external URLs, create external service discovery
        if external_urls:
            service_discoveries.append(ExternalServiceDiscovery(external_urls))

        # At least one service discovery must be provided
        if not service_discoveries:
            raise ValueError("At least one service discovery module must be configured")

        # If only one is provided, use that one directly
        if len(service_discoveries) == 1:
            return service_discoveries[0]

        # Otherwise, use combined service discovery
        return CombinedServiceDiscovery(*service_discoveries)
    else:
        raise ValueError("Invalid service discovery type")


def initialize_service_discovery(
    service_discovery_type: ServiceDiscoveryType, *args, **kwargs
) -> ServiceDiscovery:
    """
    Initialize the service discovery module with the given type and arguments.

    Args:
        service_discovery_type: the type of service discovery module
        *args: positional arguments for the service discovery module
        **kwargs: keyword arguments for the service discovery module

    Returns:
        the initialized service discovery module

    Raises:
        ValueError: if the service discovery module is already initialized
        ValueError: if the service discovery type is invalid
    """
    global _global_service_discovery
    if _global_service_discovery is not None:
        raise ValueError("Service discovery module already initialized")

    _global_service_discovery = _create_service_discovery(
        service_discovery_type, *args, **kwargs
    )
    return _global_service_discovery


def reconfigure_service_discovery(
    service_discovery_type: ServiceDiscoveryType, *args, **kwargs
) -> ServiceDiscovery:
    """
    Reconfigure the service discovery module with the given type and arguments.
    """
    global _global_service_discovery
    if _global_service_discovery is None:
        raise ValueError("Service discovery module not initialized")

    new_service_discovery = _create_service_discovery(
        service_discovery_type, *args, **kwargs
    )

    _global_service_discovery.close()
    _global_service_discovery = new_service_discovery
    return _global_service_discovery


def get_service_discovery() -> ServiceDiscovery:
    """
    Get the initialized service discovery module.

    Returns:
        the initialized service discovery module

    Raises:
        ValueError: if the service discovery module is not initialized
    """
    global _global_service_discovery
    if _global_service_discovery is None:
        raise ValueError("Service discovery module not initialized")

    return _global_service_discovery


if __name__ == "__main__":
    # Test the service discovery
    # k8s_sd = K8sServiceDiscovery("default", 8000, "release=test")
    initialize_service_discovery(
        ServiceDiscoveryType.K8S,
        namespace="default",
        port=8000,
        label_selector="release=test",
    )

    k8s_sd = get_service_discovery()

    time.sleep(1)
    while True:
        urls = k8s_sd.get_endpoint_info()
        print(urls)
        time.sleep(2)
