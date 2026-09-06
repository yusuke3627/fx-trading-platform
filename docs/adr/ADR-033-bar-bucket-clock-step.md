# ADR-033: broker 時計の逆行時に Bar バケツを開き直す

**Status:** Accepted (2026-09-07)

## Context

ADR-005 により、`BarBuilder` は broker 時計で Bar のバケツを割り当て、自分の時計で
可視化時刻を決める。これは恒常的な UTC オフセットを扱えるが、OANDA の MT5
サーバーが夏時間終了時に UTC+3 から UTC+2 へ切り替わり、broker 時計自体が 1 時間
戻る瞬間は扱えない。

開いているバケツより前の時刻を持つ quote は畳み込みにも確定にも使われない。そのため
時計が元の位置まで進む約 1 時間、すべての quote が straggler として捨てられ、Bar が
欠落する。単一の tick だけでは時計の逆行と遅着を識別できず、区別に使えるのは逆行の
幅だけである。夏時間終了時の逆行は 1 時間ちょうどである一方、フィード内の並び替えは
秒単位である。

## Decision

`BarBuilder` は、quote が開いているバケツとは別の過去バケツに属し、かつその broker
時刻が開いているバケツの最大 broker 時刻から `CLOCK_STEP_BACK` 以上前なら、遅着では
なくサーバーの時計が動いたと判断する。`CLOCK_STEP_BACK` は両者の幅の間にある 30 分と
する。

時計の逆行を検出したら、開いているバケツをその quote の `known_time` で確定して
publish し、逆行後の位置で新しいバケツを開く。閾値より小さい別バケツへの逆行は、
従来どおり straggler として捨てる。同じバケツ内での逆行は既存の畳み込み経路へ渡し、
broker 時刻が早い quote に close を置き換えさせない。

経過時間による staleness guard は採用しない。2 つの時計を直接比較せずに済む一方、
tick が来ない静かな時間帯でも「バーをいつ諦めるか」を時計の進み方に依存させる。
採用した規則は broker 時計が実際に逆行したときだけ発火する。

## Consequences

- 逆行後に繰り返される 1 時間ぶんの Bar は、既存行と
  `(symbol, timeframe, start_at)` が衝突し、`ON CONFLICT DO NOTHING` で捨てられる。
  先に書かれた行がその時点で知り得た Bar なので、既存行を維持する。tick 系列には
  両方が残るため replay は影響を受けない。
- live の `bar_service` は最後に書いた Bar の終端から tick を読み直すため、逆行した
  1 時間の tick は対象にならない。この決定が直接閉じるのは、known-time 順に tick を
  配る replay engine と、逆行を含む系列を畳むその他の経路である。
- 1 分足に対して 30 分以上遅れて届いた 1 本の quote は時計の逆行と誤認されうる。
  その場合は Bar 1 本が本来より早く確定するが、publish 済みの Bar は書き換えない。
- 逆行を跨いだ publish 順では `Bar.start` が単調でなくなる。`InMemoryMarketData` は
  publish 順に append して末尾から窓を切るので、逆行後の 1 時間は「繰り返された時刻の
  Bar が、逆行前の Bar の後ろに並ぶ」。窓の中身は実際の市場価格なので indicator は
  逆行前より新しい値を見るが、`Bar.start` を setup の同一性キーに使う戦略
  （`strategy/swing/monetary_policy_convergence.py`、
  `strategy/intraday/post_event_failed_breakout.py`）は、その 1 時間だけ既出の
  setup_id と衝突しうる。年 2 回・1 時間に限られるため、ここでは受け入れる。
- 開いているバケツは 1 つのままとする。先行 quote は従来どおり直前のバケツを確定して
  自分のバケツを開き、その後に届いた旧バケツ宛ての quote は捨てる。これを扱うには
  複数バケツと複数 Bar の戻り値が必要になり、`BarBuilder` の単純さと引き合わない。

## 実データ経路での到達性

この規則が実データで発火する経路は、現時点では存在しない。逆行を検出する条件は「quote が
開いているバケツより前のバケツに属する」ことだが、保存 tick を読み戻す経路はすべて
`ORDER BY event_time, id` で broker 時刻の昇順に返す ―― live の
`MarketTickRepository.known_before`、research の `stream_between`、いずれも同じである。
昇順に配られる限りバケツ開始は単調非減少なので、条件が成立しない。到着順で `BarBuilder`
へ配るのは `BacktestEngine.run` の `sorted(key=known_time)` だけで、これを呼ぶ
`trading.backtest.run` は合成 tick を流す。

さらに、逆行の瞬間そのものに quote が流れない。`broker_label_to_known`
（`src/trading/backtest/research.py`）が書いているとおり、サーバーは NY クローズを自分の
深夜に固定する年間一定のアンカーを持ち、New York の切り替えは 02:00 Sunday ―― FX の週末
クローズ（Friday 17:00 - Sunday 17:00 New York）の内側にある。週明けに quoting が再開する
ときには新しい quote のバケツが開いているバケツより後になるため、通常の確定経路が働く。

**ただしこの結論は、NY クローズアンカー（`market.broker_server_ahead_of_ny_hours`）が実際の
サーバー挙動と一致するという前提に依存しており、その前提自体はまだ実測していない。** 実機での
確認は micro-live 開始時の作業として残っている。アンカーが実測と食い違えば、逆行が週末クローズ
の外で起きる可能性が出る。前提が未検証である以上、ここで定義した挙動は残す。規則を外せば、
後ろ向きのステップに対する `BarBuilder` の挙動は「broker 時刻が追いつくまで全 quote を黙って
捨てる」に戻る。

同じ理由で、この決定は issue #29 を閉じない。#29 が記述した欠落が実データで起きるかどうかは、
アンカーの実測を待って判断する。
