# CA для T-Invest API на .ru

Публичные CA-сертификаты НУЦ Минцифры получены 06.10.2026 по HTTPS:

- https://gu-st.ru/content/Other/doc/russian_trusted_root_ca.cer
- https://gu-st.ru/content/Other/doc/russian_trusted_sub_ca.cer

Файлы преобразованы в PEM через `openssl x509`. Требование банка:
https://developer.tbank.ru/invest/intro/developer/network
Инструкция по CA: https://developer.tbank.ru/docs/tls-settings

| Сертификат | SHA-256 отпечаток | Срок действия до (UTC) |
| --- | --- | --- |
| Russian Trusted Root CA | `D2:6D:2D:02:31:B7:C3:9F:92:CC:73:85:12:BA:54:10:35:19:E4:40:5D:68:B5:BD:70:3E:97:88:CA:8E:CF:31` | 27.02.2032 21:04:15 |
| Russian Trusted Sub CA | `BB:BD:E2:10:3E:79:0B:99:9E:C6:2B:D0:3C:F6:25:A5:A2:E7:C3:16:E1:0A:FE:6A:49:0E:ED:EA:D8:B3:FD:9B` | 06.03.2027 11:25:19 |

`install_broker_ca.sh` сверяет отпечатки и проверяет цепочку перед установкой. Он не изменяет глобальное хранилище доверия: CA добавляются только к стандартному SSL-контексту запросов T-Invest через `TINVEST_BROKER_CA_FILE`. При выпуске новых CA нужно заново проверить официальные источники, файлы, отпечатки и сроки; отказ проверки не обходится.
