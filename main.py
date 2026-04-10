import asyncio
import edge_tts
import time
import cloudscraper
import requests
import os
import json
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse, HTMLResponse
from bs4 import BeautifulSoup
from urllib.parse import urlparse, parse_qs, unquote
from playwright.sync_api import sync_playwright
import xml.etree.ElementTree as ET

# =====================
# APP SETUP
# =====================

app = FastAPI()
HEADERS = {"User-Agent": "Mozilla/5.0"}

# Plik w którym zapisujemy datę ostatnio pobranego artykułu
STATE_FILE = "last_fetch.json"

# Pamięć podręczna artykułów — żeby nie pobierać dwa razy
_articles_cache = None

# Głos lektora - naturalny polski głos męski
VOICE = "pl-PL-MarekNeural"

# =====================
# HEALTH
# =====================

@app.get("/")
def root():
    return {"status": "ok"}

@app.get("/health")
def health():
    return {"status": "healthy"}

# =====================
# STAN APLIKACJI
# (zapamiętywanie daty ostatniego pobrania)
# =====================

def load_last_fetch_date():
    """
    Wczytuje z pliku datę ostatnio pobranego artykułu.
    Jeśli plik nie istnieje (pierwsze uruchomienie),
    zwraca datę sprzed 1 dnia.
    """
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
            return datetime.fromisoformat(data["last_date"])
    else:
        # Pierwsze uruchomienie: pobierz artykuły z ostatnich 1 dnia
        return datetime.now(timezone.utc) - timedelta(days=1)


def save_last_fetch_date(date: datetime):
    """
    Zapisuje do pliku datę ostatnio pobranego artykułu.
    """
    with open(STATE_FILE, "w") as f:
        json.dump({"last_date": date.isoformat()}, f)


# =====================
# POBIERANIE ARTYKUŁÓW Z RSS
# =====================

def extract_real_url_from_google(google_url: str) -> str:
    """Wyciąga prawdziwy URL artykułu z linku Google News (pomija consent.google.com)"""
    parsed = urlparse(google_url)
    if 'consent.google.com' in parsed.netloc:
        query_params = parse_qs(parsed.query)
        # Szukamy parametru 'continue' lub 'q'
        if 'continue' in query_params:
            raw_url = query_params['continue'][0]
            decoded_url = unquote(raw_url)  # <-- to jest kluczowe!
            return decoded_url
        elif 'q' in query_params:
            raw_url = query_params['q'][0]
            decoded_url = unquote(raw_url)
            return decoded_url
    return google_url

def resolve_google_news_url(google_url: str) -> str:
    """Dekoduje link Google News do prawdziwego URL artykułu."""
    real_url = extract_real_url_from_google(google_url)
    if real_url != google_url:
        print(f"  → Zdekodowano URL: {real_url}")
        return real_url
    else:
        print(f"  → URL bezpośredni: {google_url}")
        return google_url

def fetch_article_content(url: str) -> str:
    """Pobiera treść artykułu – akceptuje zgodę Google i przekierowuje do źródła."""
    if not url:
        return ""
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            # Przejdź do linku Google News
            page.goto(url, timeout=60000, wait_until="networkidle")
            
            # ========== OBSŁUGA STRONY ZGODY GOOGLE ==========
            if 'consent.google.com' in page.url:
                print("  → Wykryto stronę zgody Google, akceptuję...")
                # Próbuj różnych selektorów (angielski, polski, inne)
                accept_selectors = [
                    'button:has-text("Accept all")',
                    'button:has-text("Akceptuj wszystkie")',
                    'button:has-text("Zaakceptuj wszystko")',
                    'button:has-text("Zgadzam się")',
                    'button[aria-label="Accept all"]',
                    'form input[type="submit"][value*="Accept"]'
                ]
                accepted = False
                for selector in accept_selectors:
                    try:
                        if page.locator(selector).count() > 0:
                            page.locator(selector).first.click()
                            page.wait_for_timeout(3000)
                            accepted = True
                            print("  → Zgoda zaakceptowana, przechodzę dalej...")
                            break
                    except Exception as e:
                        print(f"    → Próba {selector} nie udała się: {e}")
                if not accepted:
                    print("  ⚠️ Nie znaleziono przycisku akceptacji, kontynuuję mimo to")
            # =================================================
            
            # Poczekaj na przekierowanie lub załadowanie docelowej strony
            page.wait_for_timeout(5000)
            current_url = page.url
            print(f"  → Aktualny URL: {current_url}")
            
            # Jeśli wciąż jesteśmy na Google News – spróbuj kliknąć link do artykułu
            if 'news.google.com' in current_url:
                try:
                    link = page.locator('a[href*="/articles/"]').first
                    if link:
                        link.click()
                        page.wait_for_timeout(5000)
                        current_url = page.url
                        print(f"  → Po kliknięciu: {current_url}")
                except Exception as e:
                    print(f"  → Nie udało się kliknąć linku: {e}")
            
            # Teraz powinieneś być na stronie docelowej (OSW/PISM)
            page.wait_for_selector("body", timeout=15000)
            
            # Pobierz tekst (usuwając zbędne elementy)
            content = page.evaluate("""
                () => {
                    const clone = document.body.cloneNode(true);
                    const remove = clone.querySelectorAll('script, style, nav, header, footer, aside');
                    remove.forEach(el => el.remove());
                    return clone.innerText;
                }
            """)

            from datetime import datetime
            safe_title = url.split('/')[-1] or 'article'
            with open(f"debug_content.txt", "w", encoding="utf-8") as f:
                f.write(content[:5000])
            
            if len(content) > 15000:
                content = content[:15000]
            print(f"  ✅ Pobrano treść ({len(content)} znaków)")
            return content.strip()
            
        except Exception as e:
            print(f"  ❌ Błąd Playwright: {e}")
            return ""
        finally:
            browser.close()

def fetch_rss_articles(rss_url: str, source_name: str, since: datetime) -> list:
    """
    Pobiera artykuły z RSS Google News dla danego źródła.
    Zwraca tylko artykuły nowsze niż 'since'.
    """
    try:
        r = requests.get(rss_url, timeout=10, headers=HEADERS)
        root = ET.fromstring(r.content)

        articles = []

        for item in root.findall("./channel/item"):
            title_el   = item.find("title")
            link_el    = item.find("link")
            pubdate_el = item.find("pubDate")
            source_el  = item.find("source")

            if title_el is None or pubdate_el is None:
                continue

            # Sprawdzamy czy artykuł pochodzi z właściwej domeny
            if source_el is not None:
                source_url = source_el.get("url", "")
                if source_name == "OSW" and "osw.waw.pl" not in source_url:
                    continue
                if source_name == "PISM" and "pism.pl" not in source_url:
                    continue

            title   = title_el.text.strip()
            pub_str = pubdate_el.text.strip()

            try:
                pub_date = datetime.strptime(pub_str, "%a, %d %b %Y %H:%M:%S %Z")
                pub_date = pub_date.replace(tzinfo=timezone.utc)
            except ValueError:
                continue

            if pub_date <= since:
                continue

            clean_title = title.split(" - ")[0].strip()

            if clean_title.lower() in ["publikacje", "publications", "analizy"]:
                continue

            # Pobieramy prawdziwy URL i treść artykułu
            google_link = link_el.text.strip() if link_el is not None else ""
            print(f"Pobieram treść: {clean_title}")
            real_url = resolve_google_news_url(google_link)
            content = fetch_article_content(real_url)

            articles.append({
                "title":   clean_title,
                "date":    pub_date,
                "source":  source_name,
                "content": content,
                "url":     real_url,
            })

        return articles

    except Exception as e:
        print(f"Błąd przy pobieraniu RSS ({source_name}): {e}")
        return []


def get_all_articles() -> list:
    """
    Pobiera nowe artykuły z OSW i PISM.
    Przy pierwszym uruchomieniu - ostatni 1 dzień.
    Przy kolejnych - tylko nowsze niż ostatnio pobrane.
    Wyniki są cache'owane w pamięci — kolejne wywołania
    w tej samej sesji zwracają te same artykuły.
    """
    global _articles_cache

    # Jeśli już pobraliśmy artykuły w tej sesji — zwróć z pamięci
    if _articles_cache is not None:
        return _articles_cache

    since = load_last_fetch_date()
    print(f"Pobieram artykuły nowsze niż: {since}")

    osw_articles = fetch_rss_articles(
        "https://news.google.com/rss/search?q=site:osw.waw.pl&hl=pl&gl=PL&ceid=PL:pl",
        "OSW",
        since
    )

    pism_articles = fetch_rss_articles(
        "https://news.google.com/rss/search?q=site:pism.pl&hl=pl&gl=PL&ceid=PL:pl",
        "PISM",
        since
    )

    all_articles = osw_articles + pism_articles
    all_articles.sort(key=lambda x: x["date"], reverse=True)

    if all_articles:
        save_last_fetch_date(all_articles[0]["date"])

    print(f"Pobrano artykułów: OSW={len(osw_articles)}, PISM={len(pism_articles)}")

    # Zapisz do pamięci podręcznej
    _articles_cache = all_articles
    return all_articles

# =====================
# TWORZENIE SKRYPTU PODCASTU
# =====================

def generate_article_commentary(title: str, source: str, content: str) -> str:
    """
    Używa Groq (darmowe API) żeby wygenerować naturalny komentarz
    na podstawie rzeczywistej treści artykułu.
    """
    from groq import Groq
    client = Groq(api_key=os.getenv("GROQ_API_KEY"))

    source_full = (
        "Ośrodka Studiów Wschodnich"
        if source == "OSW"
        else "Polskiego Instytutu Spraw Międzynarodowych"
    )

    if content:
        prompt = (
            f"Jesteś dziennikarzem radiowym. Na podstawie poniższej treści artykułu "
            f"z {source_full} napisz 30 zdań do podcastu. "
            f"Omów najważniejsze fakty, kontekst i znaczenie tematu. "
            f"Pisz naturalnie, jakbyś mówił do słuchacza radia. "
            f"Nie zaczynaj od słów 'Ten artykuł' ani 'Artykuł'. "
            f"Odpowiedz wyłącznie samym komentarzem, bez żadnych wstępów. "
            f"Tytuł: '{title}'\n\n"
            f"Treść artykułu:\n{content[:10000]}"
        )
    else:
        prompt = (
            f"Jesteś dziennikarzem radiowym. Na podstawie tytułu artykułu "
            f"z {source_full} napisz 5 zdań do podcastu. "
            f"Wyjaśnij kontekst i znaczenie tematu. "
            f"Pisz naturalnie, jakbyś mówił do słuchacza radia. "
            f"Nie zaczynaj od słów 'Ten artykuł' ani 'Artykuł'. "
            f"Odpowiedz wyłącznie samym komentarzem, bez żadnych wstępów. "
            f"Tytuł: '{title}'"
        )

    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        max_tokens=1200,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.choices[0].message.content


def build_podcast_script(articles: list) -> str:
    """
    Tworzy naturalnie brzmiący skrypt podcastu z komentarzami AI
    opartymi na rzeczywistej treści artykułów.
    """
    today = datetime.now().strftime("%d %B %Y")

    if not articles:
        return (
            f"Dzień dobry. Dziś jest {today}. "
            "W ciągu ostatnich dni nie opublikowano nowych analiz "
            "w Ośrodku Studiów Wschodnich ani w Polskim Instytucie "
            "Spraw Międzynarodowych. Zapraszamy jutro."
        )

    intro = (
        f"Dzień dobry. Dziś jest {today}. "
        f"Witamy w codziennym przeglądzie analiz geopolitycznych. "
        f"Dziś omówimy {len(articles)} "
        + ("publikację" if len(articles) == 1 else
           "publikacje" if len(articles) in [2, 3, 4] else "publikacji")
        + " z Ośrodka Studiów Wschodnich "
        + "oraz Polskiego Instytutu Spraw Międzynarodowych."
    )

    parts = [intro]

    for i, article in enumerate(articles, start=1):
        source_full = (
            "Ośrodek Studiów Wschodnich"
            if article["source"] == "OSW"
            else "Polski Instytut Spraw Międzynarodowych"
        )
        date_str = article["date"].strftime("%d %B")
        content  = article.get("content", "")

        print(f"Generuję komentarz AI dla: {article['title']}")
        if content:
            print(f"  → Na podstawie {len(content)} znaków treści")
        else:
            print(f"  → Brak treści, generuję na podstawie tytułu")

        commentary = generate_article_commentary(
            article["title"],
            article["source"],
            content
        )

        segment = (
            f"Publikacja {i}. "
            f"{source_full}, {date_str}. "
            f"{article['title']}. "
            f"{commentary}"
        )
        parts.append(segment)

    outro = (
        "To wszystkie publikacje na dziś. "
        "Dziękujemy za uwagę i zapraszamy jutro "
        "na kolejny przegląd analiz geopolitycznych."
    )
    parts.append(outro)

    return " ".join(parts)

# =====================
# ENDPOINTY API
# =====================

@app.get("/articles")
def list_articles():
    """Pokazuje jakie artykuły zostaną użyte do podcastu."""
    articles = get_all_articles()
    return {
        "count": len(articles),
        "articles": [
            {
                "title":  a["title"],
                "source": a["source"],
                "date":   a["date"].isoformat()
            }
            for a in articles
        ]
    }


@app.get("/podcast-script")
def podcast_script():
    """Zwraca tekstowy skrypt podcastu."""
    articles = get_all_articles()
    script = build_podcast_script(articles)
    return {"script": script}


@app.get("/generate-audio")
def generate_audio():
    file_path = "podcast.mp3"

    # Generujemy nowy podcast tylko jeśli nie ma jeszcze pliku
    if not os.path.exists(file_path):
        articles = get_all_articles()
        script   = build_podcast_script(articles)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(create_audio(script, file_path))

    return FileResponse(
        file_path,
        media_type="audio/mpeg",
        headers={"Content-Disposition": "inline"}
    )
# =====================
# GENEROWANIE AUDIO (TTS)
# =====================

async def create_audio(text: str, path: str):
    """
    Generuje audio z tekstu używając Microsoft Edge TTS.
    Głos: pl-PL-MarekNeural (naturalny, męski, polski)
    """
    communicate = edge_tts.Communicate(text, VOICE)
    await communicate.save(path)


# =====================
# STRUMIENIOWANIE AUDIO
# =====================

@app.get("/stream-audio")
def stream_audio():
    """Strumieniuje istniejący plik audio."""
    file_path = "podcast.mp3"
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Audio nie zostało jeszcze wygenerowane. Wywołaj najpierw /generate-audio")

    def iterfile():
        with open(file_path, "rb") as f:
            yield from f

    return StreamingResponse(iterfile(), media_type="audio/mpeg")


# =====================
# INTERFEJS WWW (na telefon)
# =====================

@app.get("/app", response_class=HTMLResponse)
def app_player():
    return """
    <!DOCTYPE html>
    <html lang="pl">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Poranny Podcast</title>
        <style>
            body {
                font-family: Arial, sans-serif;
                background: #1a1a2e;
                color: white;
                display: flex;
                flex-direction: column;
                align-items: center;
                padding: 40px 20px;
                min-height: 100vh;
            }
            h1 { font-size: 1.8em; margin-bottom: 8px; }
            p  { color: #aaa; margin-bottom: 30px; }
            button {
                background: #e94560;
                color: white;
                border: none;
                border-radius: 50px;
                padding: 16px 40px;
                font-size: 1.1em;
                cursor: pointer;
                margin: 10px;
                width: 260px;
            }
            button:hover   { background: #c73652; }
            button.secondary {
                background: #16213e;
                border: 1px solid #e94560;
            }
            audio { margin-top: 30px; width: 90%; max-width: 400px; }
            #status { color: #aaa; margin-top: 20px; font-size: 0.9em; }
        </style>
    </head>
    <body>
        <h1>🎙️ Poranny Podcast</h1>
        <p>Analizy OSW i PISM</p>

        <button onclick="generateAndPlay()">▶️ Generuj i Odtwórz</button>
        <button class="secondary" onclick="playExisting()">🔁 Odtwórz ostatni</button>

        <audio id="audio" controls></audio>
        <div id="status"></div>

        <script>
        function setStatus(msg) {
            document.getElementById("status").innerText = msg;
        }
        async function generateAndPlay() {
            setStatus("⏳ Generuję podcast, proszę czekać...");
            try {
                const r = await fetch("/generate-audio");
                if (!r.ok) throw new Error("Błąd generowania");
                const blob = await r.blob();
                const url  = URL.createObjectURL(blob);
                const audio = document.getElementById("audio");
                audio.src = url;
                audio.play();
                setStatus("✅ Gotowe!");
            } catch(e) {
                setStatus("❌ Błąd: " + e.message);
            }
        }
        function playExisting() {
            const audio = document.getElementById("audio");
            audio.src = "/stream-audio";
            audio.play();
            setStatus("▶️ Odtwarzam ostatni podcast...");
        }
        </script>
    </body>
    </html>
    """

if __name__ == "__main__":
    articles = get_all_articles()
    script = build_podcast_script(articles)

    import asyncio
    asyncio.run(create_audio(script, "podcast.mp3"))

    print("Podcast wygenerowany")
