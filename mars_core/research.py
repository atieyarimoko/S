import http.client
import base64
import ipaddress
import ssl
import socket
import urllib.parse
from datetime import datetime, timezone
from html.parser import HTMLParser


SEARCH_URL = "https://html.duckduckgo.com/html/"
BING_SEARCH_URL = "https://www.bing.com/search"
USER_AGENT = "MarsResearch/0.1 (+personal research assistant)"
MAX_SEARCH_BYTES = 1_500_000
MAX_PAGE_BYTES = 2_000_000
MAX_PAGE_CHARS = 16_000


class ResearchError(RuntimeError):
    pass


class SearchResultParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        classes = attributes.get("class", "").split()
        if tag == "a" and "result__a" in classes:
            self.current = {"url": attributes.get("href", ""), "title": ""}

    def handle_data(self, data):
        if self.current is not None:
            self.current["title"] += data

    def handle_endtag(self, tag):
        if tag == "a" and self.current is not None:
            self.current["title"] = " ".join(self.current["title"].split())
            self.results.append(self.current)
            self.current = None


class BingResultParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results = []
        self.in_result = False
        self.in_title = False
        self.current = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "li" and "b_algo" in attributes.get("class", "").split():
            self.in_result = True
            self.current = {"url": "", "title": ""}
        elif tag == "h2" and self.in_result and self.current is not None:
            self.in_title = True
        elif tag == "a" and self.in_title and self.current is not None and not self.current["url"]:
            self.current["url"] = attributes.get("href", "")

    def handle_data(self, data):
        if self.in_title and self.current is not None:
            self.current["title"] += data

    def handle_endtag(self, tag):
        if tag == "h2" and self.in_title:
            self.in_title = False
            if self.current and self.current["url"]:
                self.current["title"] = " ".join(self.current["title"].split())
                if self.current["title"]:
                    self.results.append(self.current)
        elif tag == "li" and self.in_result:
            self.in_result = False
            self.current = None


class PageTextParser(HTMLParser):
    IGNORED_TAGS = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form"}
    BLOCK_TAGS = {"article", "br", "div", "h1", "h2", "h3", "li", "main", "p", "section", "table", "tr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = []
        self.text = []
        self.skip_depth = 0
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.IGNORED_TAGS:
            self.skip_depth += 1
        elif tag == "title":
            self.in_title = True
        elif tag in self.BLOCK_TAGS:
            self.text.append("\n")

    def handle_endtag(self, tag):
        if tag in self.IGNORED_TAGS and self.skip_depth:
            self.skip_depth -= 1
        elif tag == "title":
            self.in_title = False
        elif tag in self.BLOCK_TAGS:
            self.text.append("\n")

    def handle_data(self, data):
        if self.skip_depth:
            return
        if self.in_title:
            self.title.append(data)
        else:
            self.text.append(data)


def _resolve_public_https(url):
    parsed = urllib.parse.urlparse(url)
    try:
        hostname = parsed.hostname
        port = parsed.port or 443
    except ValueError as error:
        raise ResearchError("閲覧先URLが不正です") from error
    if parsed.scheme != "https" or not hostname or parsed.username or parsed.password:
        raise ResearchError("閲覧先は認証情報のない公開HTTPSページに限ります")
    hostname = hostname.rstrip(".").lower()
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".localhost") or hostname.endswith(".local"):
        raise ResearchError("ローカルネットワークのページは閲覧できません")
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None:
        raise ResearchError("IPアドレスを直接指定した閲覧は許可されていません")
    try:
        addresses = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except OSError as error:
        raise ResearchError("サイトの接続先を確認できません") from error
    if not addresses:
        raise ResearchError("サイトの接続先が見つかりません")
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address[4][0].split("%", 1)[0])
        except ValueError as error:
            raise ResearchError("サイトから不正な接続先が返されました") from error
        if not ip.is_global:
            raise ResearchError("ローカルまたは予約済みIPへ解決されるサイトは閲覧できません")
    return parsed, hostname.encode("idna").decode("ascii"), port, addresses


def _validate_public_https(url):
    _resolve_public_https(url)
    return url


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, port, addresses, timeout):
        self.addresses = addresses
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())

    def connect(self):
        last_error = None
        for family, socktype, protocol, _, address in self.addresses:
            connection = socket.socket(family, socktype, protocol)
            connection.settimeout(self.timeout)
            try:
                connection.connect(address)
                self.sock = self._context.wrap_socket(connection, server_hostname=self.host)
                return
            except OSError as error:
                connection.close()
                last_error = error
        if last_error:
            raise last_error
        raise OSError("公開サイトへの接続先がありません")


def _fetch_public_https(url, max_bytes, timeout, redirects=0):
    parsed, hostname, port, addresses = _resolve_public_https(url)
    target = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    connection = PinnedHTTPSConnection(hostname, port, addresses, timeout)
    try:
        connection.request(
            "GET",
            target,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,text/plain;q=0.9",
                "Connection": "close",
            },
        )
        response = connection.getresponse()
        if response.status in {301, 302, 303, 307, 308}:
            location = response.getheader("Location")
            if not location or redirects >= 5:
                raise ResearchError("ページのリダイレクト先が不正または多すぎます")
            next_url = urllib.parse.urljoin(url, location)
            return _fetch_public_https(next_url, max_bytes, timeout, redirects + 1)
        if response.status != 200:
            raise ResearchError(f"サイトがHTTP {response.status}を返しました")
        content = response.read(max_bytes + 1)
        if len(content) > max_bytes:
            raise ResearchError("ページがサイズ上限を超えました")
        return response, content, url
    finally:
        connection.close()


def search_web(query, limit=6):
    query = " ".join(query.split())[:300]
    if not query:
        raise ResearchError("検索語が空です")
    requested_limit = max(1, min(limit, 10))
    failures = []
    providers = (
        (SEARCH_URL, SearchResultParser),
        (BING_SEARCH_URL, BingResultParser),
    )
    for base_url, parser_type in providers:
        url = base_url + "?" + urllib.parse.urlencode({"q": query})
        try:
            response, content, final_url = _fetch_public_https(url, MAX_SEARCH_BYTES, 15)
            content_type = response.headers.get_content_type()
            if content_type not in {"text/html", "application/xhtml+xml"}:
                raise ResearchError(f"検索結果の形式が不正です: {content_type}")
        except (ResearchError, TimeoutError, OSError, ssl.SSLError) as error:
            failures.append(str(error))
            continue
        parser = parser_type()
        parser.feed(content.decode("utf-8", errors="replace"))
        results = []
        for item in parser.results:
            target = urllib.parse.urljoin(final_url, item["url"])
            parameters = urllib.parse.parse_qs(urllib.parse.urlparse(target).query)
            if base_url == SEARCH_URL:
                target = parameters.get("uddg", [target])[0]
            elif "u" in parameters and parameters["u"][0].startswith("a1"):
                encoded = parameters["u"][0][2:]
                try:
                    target = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
                except (ValueError, UnicodeDecodeError):
                    continue
            parsed = urllib.parse.urlparse(target)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or not item["title"]:
                continue
            results.append({"title": item["title"][:240], "url": target})
            if len(results) >= requested_limit:
                break
        if results:
            return results
        failures.append("検索結果が見つかりませんでした")
    raise ResearchError("Web検索に失敗しました: " + "; ".join(failures))


def read_page(url):
    try:
        response, content, final_url = _fetch_public_https(url, MAX_PAGE_BYTES, 18)
        content_type = response.headers.get_content_type()
        if content_type not in {"text/html", "application/xhtml+xml", "text/plain"}:
            raise ResearchError(f"この形式のページ本文は読めません: {content_type}")
        charset = response.headers.get_content_charset() or "utf-8"
    except ResearchError:
        raise
    except (TimeoutError, OSError, ssl.SSLError) as error:
        raise ResearchError(f"ページを取得できません: {error}") from error
    decoded = content.decode(charset, errors="replace")
    if content_type == "text/plain":
        title = urllib.parse.urlparse(final_url).hostname or final_url
        text = decoded
    else:
        parser = PageTextParser()
        parser.feed(decoded)
        title = " ".join(" ".join(parser.title).split()) or urllib.parse.urlparse(final_url).hostname or final_url
        text = " ".join(" ".join(parser.text).split())
    if not text:
        raise ResearchError("ページから本文を抽出できませんでした")
    return {
        "title": title[:240],
        "url": final_url,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "text": text[:MAX_PAGE_CHARS],
    }