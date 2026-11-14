import csv
import io
import json
import math
import re
import sys
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pathlib import Path

from . import constants, log
from ast import literal_eval

load_dotenv(".env")  # loading .env file


def _error(title: str, message: str = ""):
    """Print an error message to stderr (CLI-safe replacement for tkinter showerror)."""
    log.error(f"{title}: {message}" if message else title)
def _warning(title: str, message: str = ""):
    """Print a warning message (CLI-safe replacement for tkinter showwarning)."""
    log.warning(f"{title}: {message}" if message else title)
# mode selecting

def validate_temperature(value: object) -> float:
    """Accept finite Gemini sampling temperatures in the supported 0–2 range."""
    message = "Temperature must be a finite number between 0 and 2."
    if isinstance(value, bool):
        raise ValueError(message)
    try:
        temperature = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(message) from exc
    if not math.isfinite(temperature) or not 0 <= temperature <= 2:
        raise ValueError(message)
    return temperature


def _ai_request(prompt, mode, ai_model, api_key=None, *, temperature=None) -> str | None:
    """Send a request to the AI model and return the response as a string."""

    if mode == 'default' or mode == 'flag':
        dict_ = constants.europe
    elif mode == 'world':
        dict_ = constants.world

    # request data
    prompt_to_ai = f"""Fill in a dictionary where the key """ + str(prompt) + f"""
    Search the internet and return the result as JSON. Dont write 'json' in start

    If there is no exact data for a country or continent, use the approximate value.
                
    Provide the answer only as a dictionary, without explanations.

    Dont include luxembourg
                        
    {dict_}
    """

    config = {} if temperature is None else {
        'config': types.GenerateContentConfig(temperature=validate_temperature(temperature)),
    }
    try:
        with log.spinner("Waiting for AI response..."):
            # If a key was supplied (e.g. from the web UI when .env is missing),
            # use it for this request only; otherwise fall back to the env var.
            client = genai.Client(api_key=api_key) if api_key else genai.Client()
            request = client.models.generate_content(
                model=ai_model,
                contents=prompt_to_ai,
                **config,
            )


    except Exception as e:
        _error(f"AI request failed: {e}")
        return None

    return request.text


def ai_request(prompt: str, mode: str, ai_model: str, api_key: str | None = None, _ai_answer = None,
               *, temperature: float | None = None) -> dict | None:
    "handle the AI request and parse the response into a dictionary."
    if _ai_answer is not None:
        request = _ai_answer
    else:
        options = {} if temperature is None else {'temperature': temperature}
        request = _ai_request(prompt, mode, ai_model, api_key, **options)

    # The AI request can fail and return None (the error is already logged).
    # Guard against that to avoid "AttributeError: 'NoneType' object has no attribute 'strip'".
    if not request:
        _error("AI request failed", "empty response from the AI model")
        return None

    try:
        # Models may wrap otherwise valid JSON in a Markdown code fence.
        json_style = (
            request.strip()
            .removeprefix("```json")
            .removeprefix("```python")
            .removeprefix("```")
            .removesuffix("```")
            .strip()
        )
        country_dict = json.loads(json_style)
    except json.JSONDecodeError:
        _error(f"Failed to convert into dictionary. Result {json_style}")
        print("Result:", json_style)
        return None
    except (AttributeError, TypeError) as e:
        _error("AI response parsing error", str(e))
        return None

    if not isinstance(country_dict, dict):
        _error("AI response parsing error", "expected a JSON object mapping regions to values")
        return None

    return country_dict





def load_filling():

    path = Path(__file__).parent / "filling.json"

    filling_txt = {}
    filling_num = {}

    log.info(f"Loading filling config from {path}")

    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)


        raw_txt = data.get("filling_txt", {})
        # JSON stores colors as arrays; Pillow also accepts these tuple values.
        filling_txt = {k: tuple(v) for k, v in raw_txt.items()}


        raw_num = data.get("filling_num", {})
        for key_str, color_list in raw_num.items():
            try:
                # JSON object keys are strings; recover the (low, high) range
                # with literal_eval, which parses literals without executing code.
                tuple_key = literal_eval(key_str)
                filling_num[tuple_key] = tuple(color_list)
            except (ValueError, SyntaxError):
                log.warning(f"Invalid JSON Key (see docs): {key_str}")

        log.success(
            f"Filling config loaded ({len(filling_txt)} text, {len(filling_num)} number rules)"
        )

    except Exception as e:
        _error("JSON reading error", str(e))

    

    return filling_txt, filling_num


_BULLET_PREFIX_RE = re.compile(r'^(?:[-*]|\d+[.)])\s+')
_SLUG_RE = re.compile(r'[^a-zA-Z0-9]+')


def slugify_prompt(prompt: str, max_len: int = 48) -> str:
    """Convert a map prompt into a safe, concise filename stem."""
    slug = _SLUG_RE.sub('_', prompt.strip().lower()).strip('_')
    return (slug[:max_len].strip('_') or 'map')


def _normalize_idea_entry(raw: object) -> dict[str, object] | None:
    """Normalize a string or mapping idea entry into a standard dict."""
    if isinstance(raw, str):
        prompt = raw.strip()
        return {"prompt": prompt} if prompt else None
    if isinstance(raw, dict):
        prompt = str(raw.get("prompt") or raw.get("topic") or raw.get("idea") or "").strip()
        if not prompt:
            return None
        entry: dict[str, object] = {"prompt": prompt}
        for key in ("mode", "map_type", "extra_option", "model", "font", "output", "palette_theme"):
            val = raw.get(key)
            if val is not None and str(val).strip() != "":
                entry[key] = str(val).strip()
        if raw.get("gsm") is not None and str(raw.get("gsm")).strip() != "":
            try:
                entry["gsm"] = int(raw["gsm"])
            except (TypeError, ValueError):
                pass
        return entry
    return None


def parse_ideas_content(content: str, filename: str = "") -> list[dict[str, object]]:
    """Parse map ideas from .txt, .md, .csv, or .json content."""
    text = (content or "").lstrip("\ufeff").strip()
    if not text:
        return []

    ext = Path(filename).suffix.lower() if filename else ""

    if ext == ".json" or (not ext and text[0] in "[{"):
        try:
            loaded = json.loads(text)
            if isinstance(loaded, dict) and isinstance(loaded.get("ideas"), list):
                loaded = loaded["ideas"]
            if isinstance(loaded, list):
                ideas = [item for raw in loaded if (item := _normalize_idea_entry(raw)) is not None]
                return ideas
        except json.JSONDecodeError:
            if ext == ".json":
                raise ValueError("Invalid JSON in ideas file.")

    if ext == ".csv":
        reader = csv.DictReader(io.StringIO(text))
        if reader.fieldnames:
            ideas: list[dict[str, object]] = []
            for row in reader:
                norm_row = {str(k).strip().lower(): v for k, v in row.items() if k is not None}
                entry = _normalize_idea_entry(norm_row)
                if entry is not None:
                    ideas.append(entry)
            return ideas

    ideas_txt: list[dict[str, object]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        line = _BULLET_PREFIX_RE.sub("", line).strip()
        if line:
            ideas_txt.append({"prompt": line})
    return ideas_txt


def load_ideas_from_file(file_path: str | Path) -> list[dict[str, object]]:
    """Read and parse map ideas from a .txt, .md, .csv, or .json file."""
    path = Path(file_path)
    content = path.read_text(encoding="utf-8")
    return parse_ideas_content(content, filename=path.name)




