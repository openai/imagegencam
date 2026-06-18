from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
from pathlib import Path


class OpenAIImageError(RuntimeError):
    pass


logger = logging.getLogger(__name__)


class _OpenAIClientBase:
    def __init__(self, timeout_seconds: float = 90.0, max_retries: int = 1) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(0, int(max_retries))
        self._client = None
        self._client_api_key: str | None = None

    def _require_client(self):
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise OpenAIImageError("OPENAI_API_KEY is not set.")

        if self._client is not None and self._client_api_key == api_key:
            return self._client

        try:
            from openai import OpenAI
        except ImportError as exc:
            raise OpenAIImageError(
                "The openai package is not installed. Run `pip install -r requirements.txt`."
            ) from exc

        self._client = OpenAI(
            api_key=api_key,
            timeout=self.timeout_seconds,
            max_retries=self.max_retries,
        )
        self._client_api_key = api_key
        return self._client

    @staticmethod
    def _build_image_data_url(source_path: Path) -> str:
        content_type = mimetypes.guess_type(source_path.name)[0] or "image/jpeg"
        encoded = base64.b64encode(source_path.read_bytes()).decode("ascii")
        return f"data:{content_type};base64,{encoded}"


class OpenAIImageEditor(_OpenAIClientBase):
    def __init__(
        self,
        model: str = "gpt-image-2",
        quality: str = "low",
        size: str = "1536x1024",
        output_format: str = "jpeg",
        output_compression: int = 85,
        timeout_seconds: float = 90.0,
        max_retries: int = 1,
    ) -> None:
        super().__init__(timeout_seconds=timeout_seconds, max_retries=max_retries)
        self.model = model
        self.quality = quality
        self.size = size
        self.output_format = output_format if output_format in {"png", "jpeg", "webp"} else "jpeg"
        self.output_compression = max(0, min(100, int(output_compression)))

    @property
    def output_extension(self) -> str:
        if self.output_format == "jpeg":
            return ".jpg"
        if self.output_format == "webp":
            return ".webp"
        return ".png"

    @staticmethod
    def _extract_image_bytes(result) -> bytes:
        for item in getattr(result, "data", []):
            for attribute in ("b64_json", "image_base64"):
                encoded = getattr(item, attribute, None)
                if encoded:
                    return base64.b64decode(encoded)
        raise OpenAIImageError("OpenAI returned no image data.")

    def edit_image(
        self,
        source_path: Path,
        prompt: str,
        output_path: Path,
        reference_paths: list[Path] | None = None,
        size: str | None = None,
    ) -> Path:
        client = self._require_client()
        reference_paths = [path for path in (reference_paths or []) if path.is_file()]
        requested_size = size or self.size
        if reference_paths:
            full_prompt = (
                "Use the first attached image as the main camera photo to transform. "
                "Use any additional attached images only as reference images for inspiration. "
                "Keep the first image recognizable, but apply the user's requested motif or concept "
                "using the reference image details where helpful. "
                f"User prompt: {prompt}"
            )
        else:
            full_prompt = (
                "Use the attached camera photo as the source image. "
                "Transform it according to the user's request while keeping the result coherent. "
                f"User prompt: {prompt}"
            )
        logger.info(
            "Starting image edit model=%s size=%s quality=%s format=%s source=%s refs=%s",
            self.model,
            requested_size,
            self.quality,
            self.output_format,
            source_path,
            len(reference_paths),
        )

        with source_path.open("rb") as source_file:
            reference_files = [path.open("rb") for path in reference_paths]
            request_options = {
                "model": self.model,
                "image": [source_file, *reference_files] if reference_files else source_file,
                "prompt": full_prompt,
                "quality": self.quality,
                "size": requested_size,
                "output_format": self.output_format,
                "timeout": self.timeout_seconds,
            }
            if self.model not in {"gpt-image-2", "gpt-image-2-2026-04-21"}:
                request_options["input_fidelity"] = "low"
            if self.output_format in {"jpeg", "webp"}:
                request_options["output_compression"] = self.output_compression

            try:
                result = client.images.edit(
                    **request_options,
                )
            finally:
                for reference_file in reference_files:
                    reference_file.close()

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(self._extract_image_bytes(result))
        logger.info("Saved generated image to %s", output_path)
        return output_path


class OpenAIMagicPromptPlanner(_OpenAIClientBase):
    def __init__(
        self,
        model: str = "gpt-4.1-mini",
        timeout_seconds: float = 30.0,
        max_retries: int = 1,
        title_max_length: int = 22,
    ) -> None:
        super().__init__(timeout_seconds=timeout_seconds, max_retries=max_retries)
        self.model = model
        self.title_max_length = max(8, int(title_max_length))

    def create_magic_prompt(self, reference_path: Path) -> dict[str, str]:
        client = self._require_client()
        data_url = self._build_image_data_url(reference_path)
        logger.info("Starting magic prompt planning model=%s source=%s", self.model, reference_path)
        response = client.responses.create(
            model=self.model,
            input=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "Look at this camera photo and pick one funny, visually distinct motif, prop, "
                                "detail, gesture, texture, or object that could inspire edits to future photos. "
                                "Return JSON with: "
                                "`title` = a punchy 1-3 word name, max "
                                f"{self.title_max_length} characters; "
                                "`prompt` = one concise image-edit instruction that tells an image model how to "
                                "apply that motif to another photo while keeping the new photo recognizable and coherent. "
                                "Do not mention JSON. Do not mention camera UI. Do not describe the whole image."
                            ),
                        },
                        {
                            "type": "input_image",
                            "image_url": data_url,
                            "detail": "low",
                        },
                    ],
                }
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "magic_prompt",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "title": {"type": "string"},
                            "prompt": {"type": "string"},
                        },
                        "required": ["title", "prompt"],
                    },
                }
            },
        )

        raw_output = getattr(response, "output_text", "").strip()
        if not raw_output:
            raise OpenAIImageError("OpenAI returned no magic prompt output.")

        try:
            payload = json.loads(raw_output)
        except json.JSONDecodeError as exc:
            raise OpenAIImageError(f"OpenAI returned invalid magic prompt JSON: {raw_output}") from exc

        title = str(payload.get("title") or "").strip()
        prompt = str(payload.get("prompt") or "").strip()
        if not title or not prompt:
            raise OpenAIImageError("OpenAI returned an incomplete magic prompt.")
        return {
            "title": title[: self.title_max_length].strip() or "Magic",
            "prompt": prompt,
        }
