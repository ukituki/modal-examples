import tempfile
import time
from pathlib import Path

import modal

stub = modal.Stub(name="example-voice-chatbot")


transcriber_image = (
    modal.Image.debian_slim()
    .apt_install("git", "ffmpeg")
    .pip_install(
        "https://github.com/openai/whisper/archive/v20230314.tar.gz",
        "ffmpeg-python",
    )
)


def load_audio(data: bytes, sr: int = 16000):
    import ffmpeg
    import numpy as np

    try:
        fp = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
        fp.write(data)
        fp.close()
        # This launches a subprocess to decode audio while down-mixing and resampling as necessary.
        # Requires the ffmpeg CLI and `ffmpeg-python` package to be installed.
        out, _ = (
            ffmpeg.input(
                fp.name,
                threads=0,
                format="f32le",
                acodec="pcm_f32le",
                ac=1,
                ar="48k",
            )
            .output("-", format="s16le", acodec="pcm_s16le", ac=1, ar=sr)
            .run(
                cmd=["ffmpeg", "-nostdin"],
                capture_stdout=True,
                capture_stderr=True,
            )
        )
    except ffmpeg.Error as e:
        raise RuntimeError(f"Failed to load audio: {e.stderr.decode()}") from e

    return np.frombuffer(out, np.int16).flatten().astype(np.float32) / 32768.0


class Transcriber:
    def __enter__(self):
        import torch
        import whisper

        self.use_gpu = torch.cuda.is_available()
        device = "cuda" if self.use_gpu else "cpu"
        self.model = whisper.load_model("base.en", device=device)

    @stub.function(
        gpu="A10G", container_idle_timeout=180, image=transcriber_image
    )
    def transcribe_segment(
        self,
        audio_data: bytes,
    ):
        t0 = time.time()
        np_array = load_audio(audio_data)
        result = self.model.transcribe(np_array, language="en", fp16=self.use_gpu)  # type: ignore
        print(f"Transcribed in {time.time() - t0:.2f}s")

        return result


REPO_ID = "anon8231489123/gpt4-x-alpaca-13b-native-4bit-128g"
FILENAME = "gpt4-x-alpaca-13b-ggml-q4_1-from-gptq-4bit-128g/ggml-model-q4_1.bin"
MODEL_DIR = Path("/model")


def download_model():
    from huggingface_hub import hf_hub_download

    hf_hub_download(
        local_dir=MODEL_DIR,
        repo_id=REPO_ID,
        filename=FILENAME,
    )


llama_image = (
    modal.Image.debian_slim()
    .pip_install("llama-cpp-python", "huggingface_hub")
    .run_function(download_model)
)


@stub.function(image=llama_image)
def llama():
    from llama_cpp import Llama

    llm = Llama(model_path=str(MODEL_DIR / FILENAME))

    output = llm(
        "Question: What are the names of the planets in the solar system? Answer: ",
        max_tokens=48,
        stop=["Q:", "\n"],
        echo=True,
    )

    pass


static_path = Path(__file__).with_name("frontend").resolve()


@stub.function(
    mounts=[modal.Mount.from_local_dir(static_path, remote_path="/assets")],
    container_idle_timeout=180,
)
@stub.asgi_app()
def web():
    from fastapi import FastAPI, Request
    from fastapi.staticfiles import StaticFiles

    web_app = FastAPI()
    transcriber = Transcriber()

    @web_app.post("/transcribe")
    async def transcribe(request: Request):
        bytes = await request.body()
        result = transcriber.transcribe_segment.call(bytes)
        return result["text"]

    web_app.mount("/", StaticFiles(directory="/assets", html=True))
    return web_app