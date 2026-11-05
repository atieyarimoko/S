import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_ENDPOINT = "http://127.0.0.1:11434/v1/chat/completions"


class ModelError(RuntimeError):
    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class OpenAICompatibleProvider:
    def __init__(self, endpoint, model, api_key="", timeout=90):
        parsed = urllib.parse.urlparse(endpoint)
        try:
            parsed.port
        except ValueError as error:
            raise ValueError("モデルURLのポートが不正です") from error
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("モデルURLは認証情報を含まないHTTP(S) URLにしてください")
        self.endpoint = endpoint.rstrip("/")
        if not self.endpoint.endswith("/chat/completions"):
            self.endpoint += "/chat/completions"
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    def complete(self, system, prompt):
        return self._complete(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ]
        )

    def complete_with_images(self, system, prompt, images):
        content = [{"type": "text", "text": prompt}]
        content.extend(
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{image['mime_type']};base64,"
                    + base64.b64encode(image["data"]).decode("ascii")
                },
            }
            for image in images
        )
        return self._complete(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ]
        )

    def check_connection(self):
        models_url = self.endpoint.removesuffix("/chat/completions") + "/models"
        request = urllib.request.Request(models_url, headers={"Accept": "application/json"})
        if self.api_key:
            request.add_header("Authorization", "Bearer " + self.api_key)
        try:
            with urllib.request.urlopen(request, timeout=min(self.timeout, 5)) as response:
                data = response.read(1_000_001)
            if len(data) > 1_000_000:
                raise ModelError("モデル一覧の応答が上限サイズを超えました")
            payload = json.loads(data)
            models = payload["data"]
            if not isinstance(models, list):
                raise ModelError("モデル一覧の応答形式が不正です")
            available = {
                item["id"]
                for item in models
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            }
            if self.model not in available:
                raise ModelError("ローカル推論サーバーには接続できますが、指定モデルが見つかりません")
            return True
        except urllib.error.HTTPError as error:
            raise ModelError(f"ローカル推論サーバーがHTTP {error.code}を返しました") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise ModelError(f"ローカル推論サーバーに接続できません: {error}") from error
        except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ModelError("モデル一覧の応答形式が不正です") from error

    def _complete(self, messages):
        body = json.dumps(
            {
                "model": self.model,
                "messages": messages,
                "temperature": 0.2,
                "stream": False,
            }
        ).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(self.endpoint, data=body, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise ModelError(f"モデル接続がHTTP {response.status}を返しました")
                data = response.read(2 * 1024 * 1024 + 1)
            if len(data) > 2 * 1024 * 1024:
                raise ModelError("モデル応答が上限サイズを超えました")
            payload = json.loads(data)
            content = payload["choices"][0]["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ModelError("モデルから空の応答が返りました")
            return content.strip()
        except urllib.error.HTTPError as error:
            retry_after = None
            if error.code == 429:
                retry_header = error.headers.get("Retry-After", "")
                retry_after = int(retry_header) if retry_header.isdigit() else 900
            raise ModelError(f"モデル接続がHTTP {error.code}を返しました", retry_after) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise ModelError(f"モデルに接続できません: {error}") from error
        except (KeyError, IndexError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ModelError("モデル応答の形式を読み取れません") from error


def configured_provider(environ=None):
    values = os.environ if environ is None else environ
    model = values.get("MARS_MODEL", "").strip()
    if not model:
        return None, "MARS_MODELが未設定です"
    endpoint = values.get("MARS_MODEL_URL", DEFAULT_ENDPOINT).strip()
    parsed = urllib.parse.urlparse(endpoint)
    local_hosts = {"localhost", "127.0.0.1", "::1"}
    if parsed.hostname not in local_hosts:
        return None, "継続的な無料利用を保証できない外部モデルは利用できません。Ollamaなどローカルモデルを指定してください"
    try:
        timeout = max(5, min(int(values.get("MARS_MODEL_TIMEOUT", "90")), 120))
        provider = OpenAICompatibleProvider(
            endpoint,
            model,
            values.get("MARS_MODEL_API_KEY", ""),
            timeout,
        )
    except (ValueError, TypeError):
        return None, "モデルURLまたはタイムアウト設定が不正です"
    return provider, model


def parse_json_object(text):
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
    decoder = json.JSONDecoder()
    for offset, character in enumerate(cleaned):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(cleaned[offset:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ModelError("モデル応答に有効なJSONオブジェクトがありません")