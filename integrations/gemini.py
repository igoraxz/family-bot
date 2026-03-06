"""Gemini API integration — image generation (Imagen) + image editing (Gemini native)."""

import logging
from pathlib import Path

from config import GEMINI_API_KEY, TMP_DIR

log = logging.getLogger(__name__)

# Gemini models with native image output (generation + editing)
MODEL_FLASH = "gemini-3.1-flash-image-preview"  # Fast, cheap
MODEL_PRO = "gemini-3-pro-image-preview"  # Best quality


async def generate_image(prompt: str, filename: str = "generated.png") -> dict:
    """Generate an image using Google Imagen 4.0.

    Returns dict with 'file_path' on success or 'error' on failure.
    """
    if not GEMINI_API_KEY:
        return {"error": "GEMINI_API_KEY not configured"}

    try:
        from google import genai
        from google.genai import types
    except ImportError:
        return {"error": "google-genai package not installed. Run: pip install google-genai"}

    client = genai.Client(api_key=GEMINI_API_KEY)

    try:
        response = client.models.generate_images(
            model="imagen-4.0-generate-001",
            prompt=prompt,
            config=types.GenerateImagesConfig(
                number_of_images=1,
                aspect_ratio="1:1",
                person_generation="DONT_ALLOW",
            ),
        )
    except Exception as e:
        log.error(f"Imagen API error: {e}")
        return {"error": f"Imagen API error: {str(e)[:300]}"}

    if not response.generated_images:
        return {"error": "Imagen returned no images"}

    image = response.generated_images[0].image

    file_path = TMP_DIR / filename
    file_path.write_bytes(image.image_bytes)

    log.info(f"Generated image: {file_path} ({file_path.stat().st_size / 1024:.0f}KB)")
    return {"file_path": str(file_path)}


async def edit_image(
    image_path: str,
    prompt: str,
    filename: str = "edited.png",
    use_pro: bool = False,
) -> dict:
    """Edit an existing image using Gemini's native image generation.

    Sends the image + text prompt to Gemini, which returns a modified image.
    Uses flash model by default (fast), pro model for complex edits.

    Returns dict with 'file_path' on success or 'error' on failure.
    """
    if not GEMINI_API_KEY:
        return {"error": "GEMINI_API_KEY not configured"}

    try:
        from google import genai
        from google.genai import types
        from PIL import Image
    except ImportError as e:
        return {"error": f"Missing package: {e}. Need: google-genai, Pillow"}

    src = Path(image_path)
    if not src.exists():
        return {"error": f"Source image not found: {image_path}"}

    client = genai.Client(api_key=GEMINI_API_KEY)
    model = MODEL_PRO if use_pro else MODEL_FLASH

    try:
        img = Image.open(src)
        response = client.models.generate_content(
            model=model,
            contents=[prompt, img],
            config=types.GenerateContentConfig(
                response_modalities=["TEXT", "IMAGE"],
            ),
        )
    except Exception as e:
        log.error(f"Gemini image edit error: {e}")
        return {"error": f"Gemini image edit error: {str(e)[:300]}"}

    # Extract generated image from response
    if not response.candidates or not response.candidates[0].content.parts:
        return {"error": "Gemini returned no image output"}

    for part in response.candidates[0].content.parts:
        if part.inline_data is not None:
            file_path = TMP_DIR / filename
            file_path.write_bytes(part.inline_data.data)
            log.info(f"Edited image: {file_path} ({file_path.stat().st_size / 1024:.0f}KB)")
            return {"file_path": str(file_path)}

    return {"error": "Gemini response contained no image data"}
