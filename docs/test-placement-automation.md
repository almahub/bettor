# Sistema, Agenti e automazione giocate registrate

Sistema contiene Architettura e Impostazioni. Agenti contiene 14 agenti e Agenti AI. Collegamenti diretti: #system/settings e #agents/ai.

La Dashboard mostra due controlli affiancati e indipendenti: Automazione Serie A (analisi) e Automazione Piazza Scommessa (giocate registrate). Il secondo nasce disattivato; richiede conferma esplicita per l’attivazione. Non chiama bookmaker. Conferma OPEN e addebita la Banca attraverso le normali operazioni SalaBet, un movimento per multipla. INDICATIVE resta una valutazione indicativa e non diventa ammissione operativa.

Il controllo prova subito all’attivazione, poi nei normali cicli del collector (ogni 300 secondi se attivo). Se il collector è fermo, non ci sono cicli successivi: riattivarlo separatamente. Spegnere il controllo impedisce nuove giocate automatiche, senza cancellare schedine o movimenti già registrati. Non modifica schedine manuali esistenti.

Da valutare mostra il Budget totale del turno, inizialmente 50 EUR, modificabile e persistente. Il turno è la settimana da lunedì nel fuso Europe/Rome, come nel metodo di preparazione precedente. Mostra già assegnato e residuo; non riutilizza il budget a ogni polling. Non si può ridurre sotto quanto già assegnato. Il budget non è il saldo Banca e non cambia i massimali delle ricerche OpenAI.

Viene riusato il ranking esistente e lo stesso compositore delle multiple: fino alle sei migliori partite Serie A con kickoff entro 24 ore, 3–6 partite distinte per schedina, massimo tre multiple, mercati alternativi validi, nessuna combinazione duplicata. Quote e dati provengono dalle analisi pre-match. Non si forzano alternative mancanti; la diversificazione non elimina le correlazioni.

L’importo residuo è diviso in centesimi tra le multiple composte. Saldo insufficiente, Banca non inizializzata, quote non valide, partita iniziata o errore durante la conferma annullano l’intera preparazione automatica. Schedine, addebiti e assegnazione budget condividono una transazione. Polling, restart e chiamate concorrenti non devono duplicare la spesa. Gli errori vengono conservati nella diagnostica esistente e mostrati anche nella card.

Il pulsante manuale Approva proposta e attiva preparazione mantiene il comportamento precedente: prepara senza piazzare o addebitare, anche quando l’automazione separata è attiva.

Aggiornamento0.2.5: anteprima obbligatoria e approvazione con ambito, controlli finali e stop dopo tre errori tecnici; dettagli in docs/placement-safety-and-model-diagnostics.md.
