import base64
import io
import logging
import os
import warnings

import requests
from flask import Flask, jsonify, render_template, request
from PIL import Image, ImageOps, UnidentifiedImageError

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024

PLANT_ID_API_URL = "https://api.plant.id/v3/identification"
MAX_IMAGE_PIXELS = 30_000_000
API_TIMEOUT = (10, 60)


class PlantIdError(Exception):
    def __init__(self, message, status_code=502):
        super().__init__(message)
        self.status_code = status_code


def _common_name(suggestion):
    names = suggestion.get("details", {}).get("common_names") or []
    if isinstance(names, str):
        return names
    return names[0] if names else suggestion.get("name", "Unknown plant")


def _percentage(probability):
    try:
        return round(float(probability) * 100, 1)
    except (TypeError, ValueError):
        return None


def _format_matches(suggestions, name_key="name"):
    matches = []
    for suggestion in suggestions[:3]:
        details = suggestion.get("details") or {}
        matches.append(
            {
                "name": suggestion.get(name_key) or suggestion.get("name", "Unknown"),
                "probability": _percentage(suggestion.get("probability")),
                "description": details.get("description"),
                "url": details.get("url"),
            }
        )
    return matches


def analyze_leaf_image(image):
    api_key = os.environ.get("PLANT_ID_API_KEY", "").strip()
    if not api_key:
        raise PlantIdError(
            "Plant.id API key is not configured. Set the PLANT_ID_API_KEY environment variable and restart the app.",
            503,
        )

    image_buffer = io.BytesIO()
    image.save(image_buffer, format="JPEG", quality=92)
    encoded_image = base64.b64encode(image_buffer.getvalue()).decode("ascii")
    payload = {
        "images": [encoded_image],
        "health": "all",
        "similar_images": False,
        "details": ["common_names", "description", "taxonomy", "url"],
    }

    try:
        response = requests.post(
            PLANT_ID_API_URL,
            headers={"Api-Key": api_key},
            json=payload,
            timeout=API_TIMEOUT,
        )
    except requests.Timeout as error:
        app.logger.warning("Plant.id API request timed out")
        raise PlantIdError("Plant.id took too long to analyze the image. Please try again.", 504) from error
    except requests.RequestException as error:
        app.logger.exception("Plant.id API request failed")
        raise PlantIdError("Could not reach Plant.id. Check your internet connection and try again.", 502) from error

    if response.status_code in (401, 403):
        raise PlantIdError("Plant.id rejected the API key. Check your PLANT_ID_API_KEY setting.", 503)
    if response.status_code == 429:
        raise PlantIdError(
            "Plant.id API quota or rate limit reached. Check your account and try again later.",
            429,
        )
    if not response.ok:
        app.logger.error("Plant.id returned HTTP %s: %s", response.status_code, response.text[:500])
        raise PlantIdError(f"Plant.id returned an error (HTTP {response.status_code}).", 502)

    try:
        data = response.json()
    except requests.exceptions.JSONDecodeError as error:
        app.logger.error("Plant.id returned an invalid JSON response")
        raise PlantIdError("Plant.id returned an invalid response. Please try again.", 502) from error

    if not isinstance(data, dict) or not isinstance(data.get("result"), dict):
        app.logger.error("Plant.id returned an unexpected response structure")
        raise PlantIdError("Plant.id returned an unexpected response. Please try again.", 502)

    result = data["result"]
    is_plant = result.get("is_plant") or {}
    if is_plant.get("binary") is False:
        return {
            "status": "not_plant",
            "plant": "No plant detected",
            "scientific_name": None,
            "disease": "This image does not appear to show a plant.",
            "cause": None,
            "plant_probability": _percentage(is_plant.get("probability")),
            "disease_probability": None,
            "plant_matches": [],
            "disease_matches": [],
        }

    classification = result.get("classification") or {}
    plant_suggestions = classification.get("suggestions") or []
    top_plant = plant_suggestions[0] if plant_suggestions else {}
    plant_name = top_plant.get("name")
    common_name = _common_name(top_plant) if top_plant else "Plant not identified"
    disease = result.get("disease") or {}
    disease_suggestions = disease.get("suggestions") or []
    health = result.get("is_healthy") or {}
    is_healthy = health.get("binary")
    top_disease = disease_suggestions[0] if disease_suggestions else {}
    top_disease_details = top_disease.get("details") or {}

    if is_healthy is True:
        status = "healthy"
        disease_name = "No disease detected"
    elif is_healthy is False and top_disease:
        status = "diseased"
        disease_name = top_disease.get("name", "Disease detected")
    else:
        status = "uncertain"
        disease_name = "No reliable health assessment"

    return {
        "status": status,
        "plant": common_name,
        "scientific_name": plant_name,
        "disease": disease_name,
        "cause": top_disease_details.get("description") if status == "diseased" else None,
        "plant_probability": _percentage(top_plant.get("probability")) if top_plant else None,
        "disease_probability": _percentage(top_disease.get("probability"))
        if top_disease
        else None,
        "plant_matches": _format_matches(plant_suggestions),
        "disease_matches": _format_matches(disease_suggestions),
    }


@app.route("/")
def home():
    return render_template(
        "index.html",
        api_configured=bool(os.environ.get("PLANT_ID_API_KEY", "").strip()),
    )


@app.errorhandler(413)
def file_too_large(_error):
    return jsonify({"error": "Image is too large. The maximum upload size is 16 MB."}), 413


@app.route("/predict", methods=["POST"])
def predict():
    uploaded_file = request.files.get("file")
    if uploaded_file is None or not uploaded_file.filename:
        return jsonify({"error": "Choose an image or paste one before analyzing."}), 400

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            uploaded_file.stream.seek(0)
            with Image.open(uploaded_file.stream) as image:
                image.verify()
            uploaded_file.stream.seek(0)
            with Image.open(uploaded_file.stream) as image:
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    return jsonify({"error": "Image dimensions are too large to analyze."}), 400
                rgb_image = ImageOps.exif_transpose(image).convert("RGB")
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
    ):
        return jsonify({"error": "That file is not a supported, readable image."}), 400

    try:
        return jsonify(analyze_leaf_image(rgb_image))
    except PlantIdError as error:
        return jsonify({"error": str(error)}), error.status_code


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app.run(debug=False, port=5000)
