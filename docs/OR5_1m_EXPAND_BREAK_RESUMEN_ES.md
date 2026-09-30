# Resumen OR5 1m EXPAND BREAK — para Luis

**Qué hay de nuevo:** el rango de entrada **se expande** mientras las velas 1m cierren **dentro**.

1. OR5 = High/Low 09:30–09:35 ET.
2. Después, por cada 1m:
   - Cierre **dentro** → ampliar rango (nuevo Hi/Lo).
   - Cierre **por debajo** del Low actual → SHORT al **open de la siguiente** 1m.
   - Cierre **por encima** del High actual → no short, no expandir; seguir mirando.
3. Targets y stop inicial usan el **OR5 original** (la expansión solo dispara la entrada).
4. Último 1m del día sin siguiente open → **se salta** el día.

**Gestión:** RATCHET_HI y HOLD_VWAP × T2.618/3.33/4.23. Coste 0.50. IS≤2026-06-10.

## Números clave (ALL)

| Setup | n | %W | Net | PF | pts/día | Mejor (fecha) | Peor (fecha) | delay med | exp med | w/R med | % sin entrada |
|---|---:|---:|---:|---:|---:|---|---|---:|---:|---:|---:|
| F3 EXPAND HOLD_VWAP_T423 | 46 | 41.3% | 905.1 | 1.72 | 19.7 | 483.2 (2026-06-25) | -119.0 (2026-06-11) | 2.0 | 0.00 | 1.000 | 8.0% |
| F3 EXPAND RATCHET_HI_T423 | 46 | 47.8% | 1162.1 | 1.85 | 25.3 | 483.2 (2026-06-25) | -122.0 (2026-05-26) | 2.0 | 0.00 | 1.000 | 8.0% |
| F3 FIXED BREAK HOLD_VWAP_T423 | 47 | 38.3% | 967.1 | 1.74 | 20.6 | 483.2 (2026-06-25) | -88.5 (2026-05-29) | 2.0 | — | — | 6.0% |
| F3 FIXED BREAK RATCHET_HI_T423 | 47 | 44.7% | 1056.9 | 1.66 | 22.5 | 483.2 (2026-06-25) | -147.8 (2026-06-11) | 2.0 | — | — | 6.0% |
| F3@0935 HOLD_VWAP_T423 | 50 | 34.0% | 1191.9 | 1.96 | 23.8 | 511.4 (2026-06-25) | -107.8 (2026-06-10) | 0 | — | — | 0% |
| F3@0935 RATCHET_HI_T423 | 50 | 44.0% | 1217.1 | 1.68 | 24.3 | 511.4 (2026-06-25) | -182.5 (2026-06-10) | 0 | — | — | 0% |
| ALL EXPAND HOLD_VWAP_T423 | 135 | 36.3% | 217.4 | 1.05 | 1.6 | 483.2 (2026-06-25) | -119.0 (2026-06-11) | 8.0 | 1.00 | 1.067 | 21.5% |
| ALL EXPAND RATCHET_HI_T423 | 135 | 40.0% | -309.1 | 0.94 | -2.3 | 483.2 (2026-06-25) | -138.5 (2026-06-24) | 8.0 | 1.00 | 1.067 | 21.5% |

## Respuesta directa

1. **Δnet EXPAND vs FIXED BREAK (F3 T423):** HOLD **-62.0** · HI **105.3**.
2. **Retraso entrada (desde 09:35):** F3 mediana **2.0 min** (media 18.9).
3. **Expansiones antes de entrada (F3 HI):** mediana **0.00** (media 1.43).
4. **Ancho final / R original (F3 HI):** mediana **1.000** (media 1.132).
5. **% días F3 sin entrada:** 8.0% (4 de 50).

### IS / OOS (F3 EXPAND T423)
- HOLD: IS net=565.9 PF=1.76 · OOS net=339.2 PF=1.67
- HI: IS net=558.7 PF=1.62 · OOS net=603.4 PF=2.30

Detalle EN: `OR5_1m_EXPAND_BREAK.md` · equity: `OR5_1m_EXPAND_BREAK_equity.png`  
CSV: `OR5_1m_EXPAND_BREAK_trades.csv` / `_summary.csv` / `_compare.csv`

*Research exploratorio — no es señal live.*
