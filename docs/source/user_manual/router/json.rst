.. _json:

JSON based configuration
=====================================

The router can be configured dynamically using a json file when passing the ``--dynamic-config-json`` option. The router will watch the json file for changes and update the configuration accordingly (every 10 seconds).

Currently, the dynamic config supports the following fields:

Required fields:

* ``service_discovery``: The service discovery type. Options are ``static``, ``k8s``, ``external``, or ``combined``.
* ``routing_logic``: The routing logic to use. Options are ``roundrobin`` or ``session``.


Optional fields:

* (When using static service discovery) ``static_backends``: The URLs of static serving engines, separated by commas (e.g., ``http://localhost:9001,http://localhost:9002,http://localhost:9003``).
* (When using static service discovery) ``static_models``: The models running in the static serving engines, separated by commas (e.g., ``model1,model2``).
* (When using external service discovery) ``external_backends``: The URLs of external OpenAI-compatible backends from which to auto-discover models, separated by commas (e.g., ``http://localhost:9001,http://localhost:9002``).
* (When using k8s service discovery) ``k8s_port``: The port of vLLM processes when using ``K8s`` service discovery. Default is ``8000``.
* (When using k8s service discovery) ``k8s_namespace``: The namespace of vLLM pods when using K8s service discovery. Default is ``default``.
* (When using k8s service discovery) ``k8s_label_selector``: The label selector to filter vLLM pods when using ``K8s`` service discovery.
* (When using combined service discovery) All of the above fields are optional. At least one of static, external, or k8s configuration must be provided.
* session_key: The key (in the header) to identify a session when using session-based routing.

When using combined service discovery:

* You can provide any combination of static, external, and k8s configurations, but at least one must be provided.
* If ``static_backends`` is provided, ``static_models`` must also be provided.
* ``external_backends`` will automatically discover models from the specified endpoints by querying the ``/v1/models`` endpoint.
* For k8s discovery to work, both ``k8s_namespace`` and ``k8s_port`` must be provided.

Example dynamic config files:

For static service discovery:

.. code-block:: json

    {
    "service_discovery": "static",
    "routing_logic": "roundrobin",
    "static_backends": "http://localhost:9001,http://localhost:9002,http://localhost:9003",
    "static_models": "facebook/opt-125m,meta-llama/Llama-3.1-8B-Instruct,facebook/opt-125m"
    }

For Kubernetes service discovery:

.. code-block:: json

    {
    "service_discovery": "k8s",
    "routing_logic": "roundrobin",
    "k8s_namespace": "default",
    "k8s_port": 8000,
    "k8s_label_selector": "app=vllm"
    }

For external service discovery:

.. code-block:: json

    {
    "service_discovery": "external",
    "routing_logic": "roundrobin",
    "external_backends": "http://localhost:9001,http://localhost:9002"
    }

For combined service discovery:

.. code-block:: json

    {
    "service_discovery": "combined",
    "routing_logic": "roundrobin",
    "static_backends": "http://localhost:9001,http://localhost:9002",
    "static_models": "facebook/opt-125m,meta-llama/Llama-3.1-8B-Instruct",
    "external_backends": "http://localhost:9003,http://localhost:9004",
    "k8s_namespace": "default",
    "k8s_port": 8000,
    "k8s_label_selector": "app=vllm"
    }

Get current dynamic config
--------------------------

If the dynamic config is enabled, the router will reflect the current dynamic config in the ``/health`` endpoint.

.. code-block:: bash

    curl http://<router_host>:<router_port>/health


The response will be a JSON object with the current dynamic config.

.. code-block:: json

    {
    "status": "healthy",
    "dynamic_config": "<current_dynamic_config (JSON object)>"
    }
