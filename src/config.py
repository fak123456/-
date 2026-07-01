"""Load settings from environment (.env)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Project root: parent of src/
PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    """Runtime configuration."""

    image_provider: str
    image_api_key: str
    image_api_base: str
    # If None, use config.yaml generation.size (see load_resolved_counts). If set, overrides yaml.
    output_size: str | None = None
    max_retries: int = 3
    prompts_dir: Path = PROJECT_ROOT / "prompts"
    gemini_model_id: str = "gemini-2.5-flash-image"
    openai_model_id: str = "gpt-image-1"
    doubao_model_id: str = "doubao-seedream-5-0-260128"
    doubao_api_base: str = "https://ark.cn-beijing.volces.com/api/v3"
    doubao_timeout: int = 300
    xais_model_id: str = "Nano_Banana_Pro_2K_0"
    xais_api_base: str = "https://sg2.dchai.cn"
    xais_timeout: int = 300
    shiyun_model_id: str = "gemini-2.5-flash-image"
    shiyun_api_base: str = "https://shiyunapi.com"
    shiyun_timeout: int = 300
    brief_llm_provider: str = "placeholder"
    brief_llm_api_key: str = ""
    brief_llm_api_base: str = ""


def load_settings() -> Settings:
    """Load .env from project root and return Settings."""
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path)
    else:
        load_dotenv()

    return Settings(
        image_provider=os.getenv("IMAGE_PROVIDER", "placeholder").strip().lower(),
        image_api_key=os.getenv("IMAGE_API_KEY", "").strip(),
        image_api_base=os.getenv("IMAGE_API_BASE", "").strip(),
        output_size=(
            (os.environ["IMAGE_OUTPUT_SIZE"].strip() or "native")
            if "IMAGE_OUTPUT_SIZE" in os.environ
            else None
        ),
        max_retries=int(os.getenv("IMAGE_MAX_RETRIES", "3")),
        prompts_dir=PROJECT_ROOT / "prompts",
        gemini_model_id=os.getenv("GEMINI_MODEL_ID", "gemini-2.5-flash-image").strip(),
        openai_model_id=os.getenv("OPENAI_MODEL_ID", "gpt-image-1").strip(),
        doubao_model_id=os.getenv("DOUBAO_MODEL_ID", "doubao-seedream-5-0-260128").strip(),
        doubao_api_base=os.getenv("DOUBAO_API_BASE", "https://ark.cn-beijing.volces.com/api/v3").strip(),
        doubao_timeout=int(os.getenv("DOUBAO_TIMEOUT", "300")),
        xais_model_id=os.getenv("XAIS_MODEL_ID", "Nano_Banana_Pro_2K_0").strip(),
        xais_api_base=os.getenv("XAIS_API_BASE", "https://sg2.dchai.cn").strip(),
        xais_timeout=int(os.getenv("XAIS_TIMEOUT", "300")),
        shiyun_model_id=os.getenv("SHIYUN_MODEL_ID", "gemini-2.5-flash-image").strip(),
        shiyun_api_base=os.getenv("SHIYUN_API_BASE", "https://shiyunapi.com").strip(),
        shiyun_timeout=int(os.getenv("SHIYUN_TIMEOUT", "300")),
        brief_llm_provider=os.getenv("BRIEF_LLM_PROVIDER", "placeholder").strip().lower(),
        brief_llm_api_key=os.getenv("BRIEF_LLM_API_KEY", "").strip(),
        brief_llm_api_base=os.getenv("BRIEF_LLM_API_BASE", "").strip(),
    )
