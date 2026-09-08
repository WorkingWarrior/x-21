# Generator archiwum Forum X-21

Samodzielny generator statycznej strony z dumpa bazy XenForo/MariaDB. Nie wymaga
uruchamiania XenForo ani serwera SQL i korzysta wyłącznie ze standardowej biblioteki
Pythona.

## Bezpieczeństwo

Dump XenForo zawiera prywatne dane kont, w tym adresy e-mail i dane uwierzytelniające.
Generator celowo czyta tylko sześć tabel potrzebnych do publicznego archiwum:

- `xf_node`
- `xf_forum`
- `xf_thread`
- `xf_post`
- `xf_attachment`
- `xf_attachment_data`

Nie kopiuje dumpa do katalogu wynikowego i nie odczytuje tabel z hasłami, sesjami ani
adresami e-mail. Dump należy przechowywać poza katalogiem publikowanym przez serwer WWW.

Domyślnie publikowane są wyłącznie wątki i posty ze stanem `visible`. Opcja
`--include-hidden` umieszcza również treści moderowane i usunięte; należy jej używać
wyłącznie po świadomej decyzji dotyczącej prywatności.

Forum administracyjne `Reaktor` jest zawsze domyślnie wykluczane wraz ze wszystkimi
jego wątkami i postami. Kolejne prywatne działy można wykluczyć, powtarzając opcję
`--exclude-forum "Nazwa działu"`.

## Wymagania

- Python 3.11 lub nowszy
- dump SQL utworzony przez HeidiSQL/MariaDB
- opcjonalnie katalog mediów XenForo zawierający `avatars/` i `attachments/`

## Uruchomienie

```bash
python3 generate.py \
  --dump "/ścieżka/do/db_back.sql" \
  --media-source "/ścieżka/do/data" \
  --output "/ścieżka/do/wygenerowanej-strony" \
  --base-url "https://x-21.pl"
```

Jeżeli katalog wynikowy nie jest pusty, generator przerwie pracę. Jego świadome
zastąpienie wymaga opcji `--force`. Ze względów bezpieczeństwa generator zastąpi tylko
katalog oznaczony plikiem `.x21-archive`, czyli wcześniej utworzony przez to narzędzie.
Pierwszy build należy kierować do nowego lub pustego katalogu.

W przypadku obecnej kopii archiwum parametr `--media-source` powinien wskazywać katalog
`data` zawierający podkatalogi `avatars` i `attachments`.

## Co powstaje

- `index.html` — kategorie i fora
- `forum_ID.html` — lista wątków
- `thread_ID.html` — posty wątku
- `users.html` — przeszukiwalna lista autorów publicznych treści
- `user_ID.html` i `user_ID_page_N.html` — statystyki, wątki i paginowane posty użytkownika
- `404.html`
- `assets/styles.css` — jeden, czytelny arkusz bez zewnętrznych frameworków
- `media/` — lokalna kopia dostępnych avatarów i załączników
- `build-report.json` — liczba wygenerowanych elementów i znalezionych załączników

Raport podaje także liczbę pominiętych prywatnych działów i wątków.
Profile i statystyki użytkowników są obliczane wyłącznie z opublikowanych postów i
wątków; nie wymagają odczytywania tabel kont ani prywatnych danych.

Każdy build powstaje najpierw w katalogu tymczasowym. Dopiero po wygenerowaniu i
sprawdzeniu wszystkich lokalnych odnośników zastępuje katalog wynikowy.

## Obsługiwany BBCode

Generator bezpiecznie obsługuje najczęściej występujące tagi XenForo: formatowanie
tekstu, cytaty, odnośniki, obrazy, listy, kod, spoilery, tabele, załączniki i filmy
YouTube. Nieznane tagi pozostają widoczne jako zwykły tekst albo zachowują swoją treść;
nie są wykonywane jako HTML.

## Testy

```bash
python3 -m unittest discover -s tests -v
```

Po zmianach warto dodatkowo uruchomić walidator zgodny ze standardem HTML5, np.
`html-validate`, na wygenerowanych plikach.
