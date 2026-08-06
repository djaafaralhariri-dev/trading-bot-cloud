# AI-Trading-Bot v1.0 – GitHub Cloud + Alpaca Paper

Diese Version ersetzt die Google-Cloud-VM durch GitHub Actions und GitHub Pages. Dafür ist keine Bankkarte nötig. Der Bot wird werktags während der US-Börsenzeit planmäßig gestartet, analysiert Alpaca-IEX-Minutenkurse und kann ausschließlich Paper-Bracket-Orders an Alpacas fest verdrahteten Paper-Endpunkt senden.

Die genaue Klickanleitung steht in [`README_GITHUB_CLOUD.md`](README_GITHUB_CLOUD.md).

## Neu in v1.0

- kostenloser Cloudbetrieb über GitHub Actions
- Handy- und iPad-Dashboard über GitHub Pages
- Alpaca REST-Minutenkurse statt dauerhaftem WebSocket
- Stop-Loss und Take-Profit als serverseitige Bracket-Order
- Paper-Endpunkt fest im Code
- Orders zunächst gesperrt
- maximal zwei Positionen und 75 USD je Order
- Shorts gesperrt
- öffentliche Secrets werden nicht benötigt und dürfen nicht in Dateien stehen

## Wichtige Einordnung

GitHub Actions ist kein 24/7-Server. Der Bot läuft ungefähr alle 15 Minuten während der US-Börsenzeit. GitHub kann geplante Läufe verzögern. Diese Version ist deshalb ein technischer Paper-Test, kein Echtgeldsystem.
