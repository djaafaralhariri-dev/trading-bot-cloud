# GitHub Cloud ohne Karte

Diese Version läuft nicht als dauerhafter Server. GitHub Actions startet den Bot während der US-Börsenzeit ungefähr alle 15 Minuten. Stop-Loss und Take-Profit werden als Alpaca-Paper-Bracket-Order direkt bei Alpaca gespeichert, damit sie zwischen den GitHub-Läufen aktiv bleiben.

## 1. GitHub-Repository erstellen

1. Bei GitHub anmelden oder kostenlos registrieren.
2. Rechts oben `+` → `New repository`.
3. Name: `trading-bot-cloud`.
4. **Public** wählen. Das ist für kostenlose GitHub Pages und kostenlose Standard-Actions nötig.
5. Keine README automatisch erstellen.
6. `Create repository`.

Der Quellcode wird öffentlich sichtbar. Die Alpaca-Schlüssel kommen niemals in die Dateien, sondern ausschließlich in GitHub Secrets.

## 2. Dateien hochladen

1. ZIP entpacken.
2. Im leeren GitHub-Repository `uploading an existing file` anklicken.
3. Den **gesamten Inhalt** des entpackten Ordners hineinziehen, einschließlich `.github`.
4. Unten `Commit changes`.

Nie eine lokale `.env` hochladen.

## 3. GitHub Actions Schreibrecht geben

`Settings` → `Actions` → `General` → ganz unten bei `Workflow permissions` **Read and write permissions** wählen und speichern. Das braucht nur der tägliche Scanner, damit seine neue Kandidatenliste gespeichert wird.

## 4. Alpaca-Schlüssel als Secrets speichern

Im Repository:

`Settings` → `Secrets and variables` → `Actions` → `Secrets`

Zwei Repository-Secrets erstellen:

- `ALPACA_API_KEY`
- `ALPACA_SECRET_KEY`

Nur die Keys des 250-USD-Paperkontos verwenden.

## 5. Sicherheitsvariablen einstellen

Im selben Bereich auf `Variables` wechseln und diese Repository-Variablen erstellen:

- `ALPACA_ORDER_EXECUTION` = `false`
- `MAX_OPEN_POSITIONS` = `2`
- `ALPACA_MAX_ORDER_NOTIONAL` = `75`
- `RISK_PER_TRADE` = `0.005`
- `MAX_POSITION_FRACTION` = `0.35`

Mit `false` analysiert der Bot, sendet aber noch keine Orders.

## 6. Handy-/iPad-Dashboard aktivieren

`Settings` → `Pages` → bei `Source` **GitHub Actions** auswählen.

Die spätere Adresse lautet:

```text
https://DEIN-GITHUB-NAME.github.io/trading-bot-cloud/
```

## 7. Ersten Scan starten

1. Oben auf `Actions`.
2. Links `Markt-Scanner`.
3. `Run workflow` → `Run workflow`.
4. Warten, bis der Lauf grün ist.

Der Scanner aktualisiert die Kandidatenliste. Fehlende einzelne Yahoo-Downloads sind bei kostenlosen Daten möglich und werden übersprungen.

## 8. Paper-Verbindung testen

1. Unter `Actions` links `Paper-Trading Cloud` öffnen.
2. `Run workflow` starten.
3. Nach dem grünen Lauf die GitHub-Pages-Adresse öffnen.

Dort müssen ungefähr 250 USD Paper-Equity, `Paper-Orders GESPERRT` und Alpaca IEX als Kursquelle stehen.

## 9. Paper-Orders aktivieren

Erst wenn das richtige 250-USD-Konto angezeigt wird:

`Settings` → `Secrets and variables` → `Actions` → `Variables`

`ALPACA_ORDER_EXECUTION` von `false` auf `true` ändern.

Danach erneut `Paper-Trading Cloud` manuell starten. Ab dann dürfen ausschließlich Alpaca-Paperorders gesendet werden.

Zum sofortigen Sperren die Variable wieder auf `false` setzen.

## Automatischer Ablauf

- Markt-Scanner: werktags vor US-Börsenöffnung
- Paper-Trading: während der US-Börsenzeit ungefähr alle 15 Minuten
- GitHub Pages: nach jedem Lauf aktualisiert
- Stop und Ziel: serverseitig bei Alpaca als Bracket-Order
- Shorts: gesperrt
- höchstens 2 Positionen
- höchstens 75 USD je Order
- nur ganze US-Aktien/ETFs, damit die Bracket-Orders zuverlässig geschützt sind

## Grenzen

- Das ist kein dauerhafter WebSocket-Server.
- GitHub-Zeitpläne können verspätet starten oder selten ausfallen.
- Nur die Ausstiegsorders bereits platzierter Bracket-Trades liegen dauerhaft bei Alpaca.
- Der Scanner betrachtet derzeit bis zu 300 Kandidaten, nicht jede Börse der Erde.
- Der Bot ist weiterhin nicht ausreichend für Echtgeld getestet.
- Ein öffentliches Repository darf niemals API-Schlüssel, Passwörter oder `.env` enthalten.
