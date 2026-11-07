# Bettor — identità Precisione e redesign UI

## Ambito

Redesign della presentazione: nessuna modifica a probabilità, gate, Validator,
ranking, stake, settlement, Banca, credenziali, sicurezza backup o automazioni.
La Dashboard legge il riepilogo Banca dall’endpoint GET /bank già esistente;
le operazioni economiche restano in SalaBet. Nessuna release viene pubblicata.

## Marchio e asset

Il Concept A fornito dall’utente resta il riferimento grafico. Il marchio è
ricostruito manualmente in SVG: B geometrica, tre barre ascendenti e taglio
obliquo. Non è un ritaglio raster né un’icona generica. Le curve, gli spessori
e l’inclinazione sono una ricostruzione, non una riproduzione identica del
riferimento; non vengono riprodotti i riflessi e la texture dell’immagine.
Il logotipo usa contorni Inter pesanti inclinati, con payoff vettoriale.

Asset in `better/web/brand/`:
- `bettor-logo-dark.svg` / `bettor-logo-light.svg`: marchio orizzontale e payoff;
- `bettor-symbol-dark.svg` / `bettor-symbol-light.svg`: simbolo indipendente;
- `bettor-app.svg`: icona completa su fondo antracite;
- `bettor-16.png`, `bettor-32.png`, `bettor-48.png`, `bettor-256.png`;
- `bettor.ico`: risoluzioni 16, 20, 24, 32, 40, 48, 64, 128, 256.

`favicon.svg` usa la stessa geometria. EXE PyInstaller, WebView2, configurazione
iniziale Tk e Inno Setup usano il medesimo ICO. I collegamenti ereditano l’icona
dell’EXE. Gli identificatori e i percorsi legacy rimangono compatibili.

Rigenerazione: `python scripts/generate-brand-assets.py`, con fonttools,
brotli, resvg-py e Pillow nell’ambiente di sviluppo. Non sono nuove dipendenze
del runtime. Inter è distribuito localmente con licenza OFL, senza CDN.
Fonte font: https://github.com/rsms/inter (InterVariable.woff2).

## Design system

`better/web/design-system.css` centralizza palette, spaziature, bordi, raggi,
tipografia, stati interattivi, modali, tabelle e responsive. È caricato dopo i
fogli strutturali esistenti. Tutte le superfici usano la palette richiesta.
Il rosso base #E56F76 rimane per accenti; il testo errore usa #F18A91 per
raggiungere contrasto AA anche sulla superficie elevated #253644. La variante
chiara del simbolo usa verde più scuro #087F69 per il contrasto su fondo chiaro.

Inter è self-hosted; numeri tabulari per importi, quote e probabilità. Focus
visibile, riduzione animazioni, etichette testuali per gli stati. Le icone
outline condividono dimensioni e tratto, senza nuove librerie di runtime.

La navigazione mantiene sezioni e percorsi, organizzati in Operatività,
Dati e modelli, Sistema; SalaBet è raggiungibile e distinto in ambra. In API
Control restano separate tutte le schede provider già presenti.

La Dashboard presenta quattro card operative, sei riepiloghi attività,
azioni rapide e al massimo cinque attenzioni esistenti. Le automazioni
mantengono comandi e condizioni precedenti.

Il Centro Analisi mostra tre card mercato, probabilità, fair odds e i valori
quota/edge/EV forniti dal backend. Nessun ricalcolo nel frontend. Le quote
implicite restano separate dalla previsione. ADD BET rimane unico nel Decision
Engine, con gli stessi gate. L’invio indicativo resta una proposta senza
conferma e senza movimento Banca. I dettagli tecnici rimangono espandibili.

## Verifica e distribuzione

I test UI del controller coprono tutti gli stati decisionali e i trasferimenti
esistenti; i nuovi test controllano token, icone, asset vettoriali, contrasto,
risoluzioni PNG/ICO e icona WebView2. Ispezione browser su contratti offline,
separata dal backend operativo, con viewport desktop, tablet e mobile.

Non eseguire l’installer di anteprima sull’installazione operativa senza una
richiesta esplicita. Gli asset nel sorgente non aggiornano automaticamente un
programma già installato; questo richiede un successivo aggiornamento.

## File interessati

- Web: dashboard.css/mjs, salabet.css/html/mjs, index.html, ui.mjs,
  favicon.svg, design-system.css, brand/*, fonts/*.
- Desktop: better/desktop/window.py, better/desktop/onboarding.py.
- Distribuzione: packaging/Better.spec, packaging/Better.iss.
- Generazione: scripts/generate-brand-assets.py.
- Regression: tests/ui/test_design_system.mjs, test_dashboard.mjs,
  test_product_name.mjs; tests/unit/test_brand_assets.py;
  tests/integration/test_salabet_api.py (titolo della pagina aggiornato).

## Collaudo visuale del redesign

Controllate dieci viste a 1440, 1024 e 390 px (30 combinazioni): Dashboard,
analisi con i tre mercati, Modelli, Pipeline, API Control, Agenti, candidate,
Banca, scommesse aperte e Archivio. Nessun overflow della pagina o errore
JavaScript rilevato sui contratti dimostrativi. Inter risulta caricato
localmente a tutte le larghezze. Le tabelle larghe scorrono nel contenitore.
Questa verifica offline non pretende di sostituire un collaudo con tutti i
possibili dati reali o una prova interattiva completa con screen reader.

Build EXE verificata con nove risorse icona Windows e asset CSS/ICO identici
ai sorgenti. Setup di anteprima compilato in una directory separata sotto
.cache/precision-installer, con checksum SHA256; non installato n? pubblicato.
Il wizard iniziale ? stato verificato senza completare alcuna configurazione.

Esito suite completa: 714 test Python e 32 test UI superati (746 totali).
Sei nuovi regression test, oltre all?aggiornamento delle aspettative UI.
Due warning gi? presenti: deprecazione TestClient/httpx e ZIP duplicato nel
test intenzionale di manomissione backup. Nessun fallimento.
