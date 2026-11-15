import csv
import io
import json
import math
import re
import sys
try:
    import httpx2 as httpx
except ImportError:  # Older google-genai releases use the original transport.
    import httpx
from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pathlib import Path

from . import constants, log
from ast import literal_eval

load_dotenv(".env")  # loading .env file

AI_TIMEOUT_MS = 45_000


class AIServiceError(RuntimeError):
    """An actionable upstream failure safe to display in the web interface."""

    def __init__(self, message, status_code=503):
        super().__init__(message)
        self.status_code = status_code


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
            options = {'http_options': types.HttpOptions(
                timeout=AI_TIMEOUT_MS, retry_options=types.HttpRetryOptions(attempts=1),
            )}
            if api_key:
                options['api_key'] = api_key
            client = genai.Client(**options)
            request = client.models.generate_content(
                model=ai_model,
                contents=prompt_to_ai,
                **config,
            )


    except genai_errors.APIError as error:
        if error.code in (500, 502, 503, 504):
            raise AIServiceError(
                f'The selected model ({ai_model}) is overloaded or temporarily unavailable. '
                'Please try again shortly or select another model.', 503,
            ) from error
        if error.code == 429:
            raise AIServiceError(
                'The AI request limit or quota has been reached. Please try again later '
                'or check the quota for your API key.', 429,
            ) from error
        if error.code in (400, 401, 403, 404):
            raise AIServiceError(
                f'The selected model ({ai_model}) could not be used. '
                'Refresh the model list and check your API key and model access.', 400,
            ) from error
        raise AIServiceError('The AI service could not complete this request. Please try again.') from error
    except (httpx.TimeoutException, TimeoutError) as error:
        raise AIServiceError(
            'The AI model did not respond within 45 seconds. '
            'Please try again or select a faster model.', 504,
        ) from error
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
    if not path.is_file():
        path = path.with_name("filling.template.json")

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


_SLUG_RE = re.compile(r'[^a-zA-Z0-9]+')

BATCH_CSV_REQUIRED_COLUMNS: tuple[str, ...] = ("prompt",)
BATCH_CSV_OPTIONAL_COLUMNS: tuple[str, ...] = (
    "mode",
    "map_type",
    "gsm",
    "extra_option",
    "model",
    "font",
    "palette_theme",
    "output",
)
BATCH_CSV_ALL_COLUMNS: tuple[str, ...] = BATCH_CSV_REQUIRED_COLUMNS + BATCH_CSV_OPTIONAL_COLUMNS
_ALLOWED_MODES = frozenset({"text", "number"})
_ALLOWED_MAP_TYPES = frozenset({"default", "flag", "world"})
_ALLOWED_EXTRA_OPTIONS = frozenset({"none", "show_most_least", "short_form"})


def slugify_prompt(prompt: str, max_len: int = 48) -> str:
    """Convert a map prompt into a safe, concise filename stem."""
    slug = _SLUG_RE.sub('_', prompt.strip().lower()).strip('_')
    return (slug[:max_len].strip('_') or 'map')


def parse_ideas_content(content: str, filename: str = "") -> list[dict[str, object]]:
    """Parse and validate map ideas from a .csv file."""
    if filename and Path(filename).suffix.lower() != ".csv":
        raise ValueError("Only .csv files are supported for batch ideas.")

    text = (content or "").lstrip("\ufeff").strip()
    if not text:
        raise ValueError("CSV file is empty.")

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV file is missing a header row.")

    headers = [str(h).strip().lower() for h in reader.fieldnames]
    if any(not h for h in headers):
        raise ValueError("CSV header contains an empty column name.")
    if len(set(headers)) != len(headers):
        raise ValueError("CSV header contains duplicate column names.")

    unknown_cols = [h for h in headers if h not in BATCH_CSV_ALL_COLUMNS]
    if unknown_cols:
        raise ValueError(
            f"Unknown CSV column(s): {', '.join(unknown_cols)}. "
            f"Allowed columns: {', '.join(BATCH_CSV_ALL_COLUMNS)}."
        )

    missing_cols = [c for c in BATCH_CSV_REQUIRED_COLUMNS if c not in headers]
    if missing_cols:
        raise ValueError(f"Missing required CSV column(s): {', '.join(missing_cols)}.")

    reader.fieldnames = headers
    ideas: list[dict[str, object]] = []
    for row_num, row in enumerate(reader, start=2):
        if None in row:
            raise ValueError(f"Row {row_num}: malformed CSV row (unexpected extra columns).")

        prompt = str(row.get("prompt") or "").strip()
        if not prompt:
            raise ValueError(f"Row {row_num}: 'prompt' is required and cannot be empty.")

        entry: dict[str, object] = {"prompt": prompt}

        mode_val = str(row.get("mode") or "").strip().lower()
        if mode_val:
            if mode_val not in _ALLOWED_MODES:
                raise ValueError(
                    f"Row {row_num}: invalid mode '{mode_val}'. Allowed: {', '.join(sorted(_ALLOWED_MODES))}."
                )
            entry["mode"] = mode_val

        map_type_val = str(row.get("map_type") or "").strip().lower()
        if map_type_val:
            if map_type_val not in _ALLOWED_MAP_TYPES:
                raise ValueError(
                    f"Row {row_num}: invalid map_type '{map_type_val}'. Allowed: {', '.join(sorted(_ALLOWED_MAP_TYPES))}."
                )
            entry["map_type"] = map_type_val

        extra_val = str(row.get("extra_option") or "").strip().lower()
        if extra_val:
            if extra_val not in _ALLOWED_EXTRA_OPTIONS:
                raise ValueError(
                    f"Row {row_num}: invalid extra_option '{extra_val}'. Allowed: {', '.join(sorted(_ALLOWED_EXTRA_OPTIONS))}."
                )
            entry["extra_option"] = extra_val

        gsm_raw = str(row.get("gsm") or "").strip()
        if gsm_raw:
            try:
                gsm_val = int(gsm_raw)
            except ValueError as exc:
                raise ValueError(
                    f"Row {row_num}: invalid gsm '{gsm_raw}'. Must be an integer between 1 and 100."
                ) from exc
            if gsm_val < 1 or gsm_val > 100:
                raise ValueError(
                    f"Row {row_num}: invalid gsm '{gsm_raw}'. Must be an integer between 1 and 100."
                )
            entry["gsm"] = gsm_val

        for key in ("model", "font", "palette_theme", "output"):
            val = str(row.get(key) or "").strip()
            if val:
                entry[key] = val

        ideas.append(entry)

    if not ideas:
        raise ValueError("CSV file contains no data rows.")

    return ideas


def load_ideas_from_file(file_path: str | Path) -> list[dict[str, object]]:
    """Read and parse map ideas from a .csv file."""
    path = Path(file_path)
    if path.suffix.lower() != ".csv":
        raise ValueError("Only .csv files are supported for batch ideas.")
    content = path.read_text(encoding="utf-8")
    return parse_ideas_content(content, filename=path.name)




