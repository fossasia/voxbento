# Adding New ML Models to Voxbento

Voxbento delegates all heavy Machine Learning workloads (Transcription, Translation, TTS) to a dedicated **Ray Serve** cluster. This completely decouples the GPU-intensive inference tasks from the real-time FastAPI web server.

If you want to add a new open-source model (e.g., a local TTS engine or an LLM for summarization), follow these steps.

---

## Step 1: Write the Deployment Class

Create a new Python file in the `portal/ray_serve/` directory (e.g., `portal/ray_serve/tts.py`). 

Your class must:
1. Be decorated with `@serve.deployment`
2. Initialize the heavy model weights in `__init__`
3. Implement `async def __call__(self, request: Request)` to handle the incoming HTTP JSON payload.

> [!IMPORTANT]
> **Dynamic Hardware Support:** Do **NOT** hardcode `device="cpu"` or `ray_actor_options={"num_cpus": 2}`. You must dynamically read the assigned resources from Ray so that the code automatically scales when deployed to a GPU server.

### Example Template

```python
import logging
from ray import serve
from starlette.requests import Request

logger = logging.getLogger(__name__)


@serve.deployment
class TTSGenerator:
    def __init__(self, model_name: str = "example-tts"):
        import ray

        # 1. Dynamically check for GPUs
        has_gpu = len(ray.get_gpu_ids()) > 0
        self.device = "cuda" if has_gpu else "cpu"
        self.compute_type = "float16" if has_gpu else "int8"

        # 2. Dynamically check assigned CPUs to prevent thread locking
        self.assigned_cpus = int(ray.get_runtime_context().get_assigned_resources().get("CPU", 2))
        self.intra_threads = max(1, self.assigned_cpus)

        # 3. Load the model using dynamic parameters
        logger.info(f"Loading {model_name} on {self.device}")
        # self.model = YourModel(device=self.device, threads=self.intra_threads)

    async def __call__(self, request: Request):
        payload = await request.json()

        text = payload.get("text", "")
        if not text:
            return {"error": "Missing text in payload."}

        # Perform inference
        # audio_bytes = self.model.generate(text)

        return {"audio_b64": "base64_encoded_audio_here"}


# Bind the deployment so Ray Serve can import it
tts_app = TTSGenerator.bind()
```

---

## Step 2: Register it in `serve_config.yaml`

Ray Serve uses `serve_config.yaml` as the absolute single source of truth for the entire cluster. You must register your new application here.

1. Set the `route_prefix` (the HTTP endpoint).
2. Set the `import_path` to point to the `.bind()` variable you created in Step 1.
3. Explicitly define hardware allocations in the `deployments` block.

```yaml
  - name: tts
    route_prefix: /tts
    import_path: portal.ray_serve.tts:tts_app
    deployments:
      - name: TTSGenerator
        autoscaling_config:
          min_replicas: 0
          initial_replicas: 0
          max_replicas: 2
          target_num_ongoing_requests_per_replica: 5
        ray_actor_options:
          num_cpus: 2
          num_gpus: 0
```

> [!TIP]
> **GPU Deployment:** When you want to run this model on a GPU, simply change `num_gpus: 0` to `num_gpus: 1` in this file. You do **not** need to touch the Python code!

---

## Step 3: Call it from FastAPI

Because Ray Serve handles its own HTTP proxy, the FastAPI web server does not need to import any heavy ML libraries. You simply invoke the model over the network using Voxbento's built-in `RayClient`.

```python
from portal.ray_serve.client import RayClient


async def generate_speech(text: str):
    client = RayClient()
    try:
        # The endpoint name must match the `route_prefix` from Step 2 (without the slash)
        result = await client.predict("tts", payload={"text": text})

        if "error" in result:
            print(f"Model Error: {result['error']}")
            return None

        return result["audio_b64"]

    except Exception as e:
        print(f"Failed to reach Ray Serve: {e}")
```
