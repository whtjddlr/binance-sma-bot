# Binance SMA 자동매매 스타터

Binance 현물에서 단순이동평균(SMA) 교차를 실행하는 Python 프로젝트입니다. 기본값은 **실제 주문을 전혀 내지 않는 paper 모드**이며, BTC/USDT 1시간봉의 SMA 20/50을 사용합니다.

> 이 코드는 수익을 보장하는 상품이 아니라 학습·검증용 스타터입니다. 이동평균 교차는 횡보장에서 반복 손실이 날 수 있고, 시장가 주문에는 수수료와 슬리피지가 발생합니다.

## 현재 동작

- Binance Spot 롱 전용: 선물, 레버리지, 공매도, 출금 기능 없음
- 골든크로스에서 고정 USDT 금액 매수, 데드크로스에서 봇 장부 수량만 매도
- Binance 서버 시각을 기준으로 **마감된 캔들만** 계산
- 첫 실행은 기준점만 저장하고 과거 교차를 뒤늦게 매수하지 않음
- 같은 캔들 중복 처리와 두 프로세스 동시 실행 차단
- 주문 요청 전 의도를 저장하고, 응답 유실 시 같은 주문을 자동 재전송하지 않음
- 미확정 주문은 `origClientOrderId`와 체결 내역으로 조회해 완전·부분 체결 및 미체결 종료를 복구
- 거래소의 현재 최소 주문금액·수량·정밀도 규칙을 주문 직전에 새로 확인
- 신규 매수용 일일 실현손실 회로 및 선택형 로컬 손절

Binance 공식 문서상 Spot Testnet 자산은 전부 가상이며 입출금할 수 없고, 주문·잔고 데이터는 사전 고지 없이 대략 월 1회 초기화될 수 있습니다. [Spot Test Network 안내](https://developers.binance.com/en/docs/products/spot/testnet/general-info)

프로그램은 매도 직전 봇의 원주문이 Testnet에 남아 있는지 확인합니다. Testnet 초기화나 API 키 변경으로 원주문·로컬 장부가 일치하지 않으면 자동 매도를 중지합니다. 초기화 후에는 기존 상태 파일을 임의로 재사용하지 말고 거래 내역과 잔고를 확인한 뒤 새 `STATE_FILE`로 다시 시작하세요.

## 빠른 시작: paper 모드

Python 3.11 이상이 필요합니다.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
python -m pip install -e '.[dev]'
cp .env.example .env            # Windows에서는 파일을 직접 복사
sma-bot check
sma-bot once
sma-bot status
```

첫 `once`는 최신 확정봉을 기준점으로만 저장합니다. 계속 실행하려면 다음 명령을 사용합니다.

```bash
sma-bot run
```

종료는 `Ctrl+C`입니다. 기본 paper 잔액은 1,000 USDT이며 실시간 Binance 공개 시세로 가상 체결합니다.

## 과거 데이터 백테스트

API 키 없이 Binance 본망의 공개 과거 Kline을 내려받아 시뮬레이션할 수 있습니다.

```bash
sma-bot backtest --since 2024-01-01 --until 2025-12-31
```

날짜만 쓰면 UTC 기준이며 `--until 2025-12-31`은 해당 날짜 전체를 포함합니다. 시각까지 지정할 때는 `Z` 또는 `+09:00` 같은 시간대를 반드시 붙이세요. 시작·종료를 생략하면 최근 365일을 사용합니다.

```bash
sma-bot backtest \
  --since 2024-01-01T00:00:00Z \
  --until 2025-01-01T00:00:00Z \
  --starting-balance 1000 \
  --quote-amount 25 \
  --fee-rate 0.001 \
  --slippage-bps 5 \
  --trades-csv ./data/backtest-trades.csv
```

백테스트의 중요한 가정은 다음과 같습니다.

- 신호는 확정봉 종가로 계산하고 **다음 봉 시가**에 체결해 미래정보 누수를 막습니다.
- 매수·매도 양쪽에 수수료와 슬리피지를 적용합니다. 기본 슬리피지는 편도 5 bps입니다.
- 실시간 봇처럼 한 번에 `QUOTE_AMOUNT`만 투자하고, 한 포지션만 보유합니다.
- 마지막에 열린 포지션은 강제 매도하지 않고 마지막 종가로 평가합니다.
- 시작일 이전 SMA 워밍업 데이터를 자동으로 받지만 성과에는 포함하지 않습니다.
- 기본 최대 거래 구간은 50,000봉입니다. 더 길면 `--max-candles`를 늘리거나 기간을 나누세요.
- 캔들 누락·중복·역순이 발견되면 임의 보간하지 않고 중지합니다.

출력에는 최종평가액, 총수익률, MDD, 완료 거래 수, 승률, 실현·미실현손익과 수수료가 포함됩니다. 과거 필터·호가 깊이·부분체결은 완전히 재현하지 못하므로 결과는 실제 수익을 보장하지 않습니다. Binance Kline은 요청당 최대 1,000봉이므로 프로그램이 자동으로 페이지를 나눠 받습니다. [Binance Kline 문서](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/market)

실시간 paper 모드는 확정봉 종가에서 즉시 가상 체결하지만, 백테스트는 더 보수적인 다음 봉 시가 체결을 사용하므로 두 결과의 체결가는 의도적으로 다릅니다.

## Spot Testnet 연결

1. [Binance Spot Test Network](https://testnet.binance.vision/)에 로그인해 Testnet API 키를 만듭니다.
2. 키에 거래 권한만 활성화합니다. 실제 운영 키에는 출금 권한을 절대 부여하지 마세요.
3. `.env`를 다음처럼 변경합니다.

```dotenv
TRADING_MODE=testnet
BINANCE_API_KEY=테스트넷_API_KEY
BINANCE_API_SECRET=테스트넷_API_SECRET
STATE_FILE=./data/testnet-btcusdt-1h.json
```

그다음 설정과 계정을 확인하고 한 번만 실행합니다.

```bash
sma-bot check
sma-bot once
sma-bot status
```

Testnet도 `MARKET` 주문은 실제 Testnet 매칭 엔진에 전달됩니다. Binance는 시장가 매수에 `quoteOrderQty`를 지원하며, 이 프로젝트는 `QUOTE_AMOUNT`를 그 방식으로 지출 상한에 사용합니다. [Testnet 주문 API](https://developers.binance.com/en/docs/products/spot/testnet/rest-api#trading-endpoints)

## 전략 정의

직전 확정봉과 최신 확정봉의 두 SMA를 비교합니다.

```text
매수: 이전 SMA_fast <= 이전 SMA_slow, 현재 SMA_fast > 현재 SMA_slow
매도: 이전 SMA_fast >= 이전 SMA_slow, 현재 SMA_fast < 현재 SMA_slow
```

SMA 20/50 교차에는 최소 51개의 확정 종가가 필요합니다. REST 캔들은 open time으로 식별되므로, 프로그램은 `open_time + timeframe <= Binance server time`인 봉만 사용합니다. Kline 규격과 제한은 [Binance Kline 문서](https://developers.binance.com/en/docs/products/spot/testnet/rest-api#market-data-endpoints)를 참고하세요.

## 주요 설정

| 환경변수 | 기본값 | 의미 |
|---|---:|---|
| `TRADING_MODE` | `paper` | `paper` 또는 `testnet` |
| `SYMBOL` | `BTC/USDT` | USDT 현물 거래 페어 |
| `TIMEFRAME` | `1h` | 고정 길이 캔들 주기(`1m`~`1w`, 월봉 제외) |
| `FAST_PERIOD` | `20` | 단기 SMA 기간 |
| `SLOW_PERIOD` | `50` | 장기 SMA 기간 |
| `QUOTE_AMOUNT` | `25` | 매수 1회당 USDT |
| `POLL_SECONDS` | `30` | 조회 간격, 최소 5초 |
| `MAX_SIGNAL_AGE_SECONDS` | `600` | 오래된 교차 신호 주문 방지, 0은 비활성 |
| `MAX_DAILY_LOSS_USDT` | `10` | 당일 실현손실 도달 시 신규 매수 차단, 0은 비활성 |
| `CLOSE_STOP_LOSS_PCT` | `0` | 확정봉 종가 기준 로컬 손절, 0은 비활성 |
| `STATE_FILE` | `./data/state.json` | 봇 장부와 중복 방지 상태 |

모드, API 계정, 심볼, 시간봉, SMA 기간을 변경할 때는 별도의 `STATE_FILE`을 사용하세요. 상태는 API 키의 비복원 지문과 결합되며, 키가 달라지면 실행을 차단합니다. 기존 봇 포지션이 있는 상태 파일은 절대로 삭제하거나 임의 편집하지 마세요.

## 실제 자금 주문

이 초안은 `paper`와 Binance Spot `testnet`만 허용하며 실제 자금 주문 경로는 의도적으로 포함하지 않았습니다. Testnet에서 주문·수수료·재시작 복구를 충분히 검증한 뒤, 전용 계정/서브계정, API IP 제한, 출금 권한 비활성화, 거래 원장 DB와 거래소 측 보호 주문을 포함해 별도 단계로 추가해야 합니다.

## 안전장치의 한계

- `MAX_DAILY_LOSS_USDT`는 로컬 장부의 **실현손익 기반 신규 매수 차단**입니다. 미실현손실을 포함한 최대 손실 보장이 아닙니다.
- `CLOSE_STOP_LOSS_PCT`는 확정봉 종가를 한 번 평가하는 로컬 규칙입니다. 장중 손절이나 거래소 측 보호 주문이 아니며, 프로그램·네트워크 중단과 갭·슬리피지로 손실률을 보장할 수 없습니다.
- 시장가 주문의 HTTP 타임아웃이나 5xx는 실패 확정이 아닐 수 있습니다. Binance도 원주문 상태를 먼저 조회하도록 안내하므로, 이 봇은 미확정 상태에서 자동 재주문하지 않습니다. [Binance 주문 API](https://developers.binance.com/en/docs/products/spot/testnet/rest-api#trading-endpoints)
- 최소 주문금액, `LOT_SIZE`, `MARKET_LOT_SIZE`, `MIN_NOTIONAL`/`NOTIONAL`은 바뀔 수 있어 주문 직전에 거래소 메타데이터를 새로 읽습니다. [Binance 필터](https://developers.binance.com/en/docs/products/spot/testnet/filters)
- 정밀도 내림 뒤 팔 수 없는 극소량은 상태의 `dust_qty`/`dust_cost`에 별도로 남깁니다. 장부보다 실제 가용 잔액이 작으면 일부만 임의 매도하지 않고 중지합니다.
- JSON 상태 파일은 단일 PC·단일 프로세스 스타터용입니다. 큰 금액이나 다중 서버 운영에는 거래 원장 DB, User Data Stream, 알림, 백업, 장애 주입 테스트가 추가로 필요합니다.

## 테스트

```bash
python -m pytest
```

테스트에는 골든/데드크로스, SMA 경계값, 데이터 부족, 최초 워밍업, 같은 봉 중복, 오래된 진입 차단과 위험 축소 매도, 가상 매수·매도, 부분체결·수수료·dust 복구, 상태 파일 전략/API 계정 결합, 과거 Kline 페이지네이션, 다음 봉 시가 체결, 슬리피지·MDD, 캔들 누락, Testnet 초기화 차단, 프로세스 잠금이 포함됩니다. Testnet API 키를 요구하는 실제 주문 테스트는 의도적으로 자동 실행하지 않습니다.

ccxt의 Sandbox 전환은 다른 API 호출보다 먼저 수행하도록 구성했습니다. 관련 동작은 [ccxt 공식 매뉴얼](https://github.com/ccxt/ccxt/wiki/manual#testnets-and-sandbox-environments)에서 확인할 수 있습니다.

## 메이저 코인 연구 최적화

`research_portfolio_backtest.py`와 `research_optimizer.py`는 실거래 봇과 분리된 연구 도구입니다. BTC, ETH, BNB, SOL, XRP, ADA, DOGE, LINK 일봉을 이용해 SMA, EMA, 돌파 전략과 동일가중·역변동성 배분을 비교합니다. 개발구간에서만 후보를 순위화하고 검증구간, 최종 미사용 구간, 비용 2배 조건을 합격 관문으로 사용합니다. 결과가 좋아도 실거래 설정을 자동 변경하지 않습니다.

```bash
PYTHONPATH=src python research_optimizer.py \
  --data-dir data/research \
  --samples 3780 \
  --output data/optimizer-results-full.json
```

새 일봉이 추가됐을 때만 데이터를 갱신하고 재탐색하려면 다음 연구 루프를 별도 서버나 작업 스케줄러에서 실행합니다.

```bash
PYTHONPATH=src python continuous_research.py --interval-hours 24
```

대화형 작업공간은 영구 서버가 아니므로 장기 실행에는 지속형 VM, 컨테이너 서비스 또는 cron이 필요합니다. 반복 최적화는 과최적화 위험을 없애지 않으며, 최종 미사용 구간을 본 뒤 같은 구간에 맞춰 설정을 다시 선택하면 더 이상 미사용 검증이 아닙니다.
