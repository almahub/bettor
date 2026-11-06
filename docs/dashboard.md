# Dashboard e navigazione Bettor

La pagina iniziale è Dashboard. Mostra Pipeline, Collector e Model Validator, controlli separati per automazione analisi Serie A e registrazione delle giocate, attività recenti, schedine da confermare e massimo cinque attenzioni operative. Le metriche tecniche dei provider rimangono in API Control e Data Pipeline.

Un riepilogo segnala le configurazioni iniziali mancanti e rimanda a Sistema → Impostazioni. La card delle schedine DRAFT/READY mostra il termine del primo kickoff meno quindici minuti; il promemoria non conferma né piazza nulla.

Navigazione: Dashboard, Analisi, Journal, Modelli e validazione, Data Pipeline, Sistema e Agenti. SalaBet è un workspace distinto; Banca è presente solo lì.

- **Sistema:** Architettura, Impostazioni, Backup, Aggiornamenti.
- **Agenti:** 14 agenti, Agenti AI, API Control.
- **SalaBet:** Da valutare, Scommesse aperte, Archivio e Banca.

La UI usa HTML/CSS e moduli JavaScript nativi. L'app installata li mostra in una finestra WebView2, mantenendo il backend locale. Gli asset JavaScript hanno MIME esplicito. I dati del modello e gli errori mantengono reason code leggibili; uno stato configurato non equivale a una verifica live del provider.

Il Journal conserva snapshot e provenienza: la paginazione limita la schermata senza troncare il registro. Le viste della coorte e della maturità filtrano e paginano lo stato ricostruito dai dati persistenti.
