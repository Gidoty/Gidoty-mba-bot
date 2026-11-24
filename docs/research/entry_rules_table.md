# Entry-rule study: results

Plan hash `6e9a567b49a39786`. Nine test windows of six months, 2022-04 to 2026-09, 1h candles, BTC, ETH, BNB, XRP pooled, own exits (no profit rules), 5 bps taker per side, 11 bps round-trip slippage, historical funding. 99% month-block bootstrap intervals. Trades are assigned to a window by entry time.

Pass rule (fixed in advance): P1 at least 300 trades; P2 mean net R above zero and the interval of the pooled total above zero; P3 net total R above zero in at least 7 of 9 windows; P4 the mean beats the unfiltered strategy with the interval of the difference above zero.

## ema

| candidate | trades | mean net R | total net R | total R interval | windows > 0 (of 9) | mean vs unfiltered [interval] | P1 | P2 | P3 | P4 | edge survives (P1-P3) | passes all |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| none | 2659 | -0.123 | -327.5 | [-757.2, +155.5] | 4 | +0.000 [+0.000, +0.000] | yes | no | no | no | no | no |
| gap | 8 | -0.141 | -1.1 | [-8.8, +21.4] | 1 | -0.018 [-1.122, +2.888] | no | no | no | no | no | no |
| htf4 | 1044 | -0.136 | -142.4 | [-384.7, +130.1] | 3 | -0.013 [-0.203, +0.170] | yes | no | no | no | no | no |
| htfD | 1276 | -0.168 | -214.3 | [-501.9, +114.7] | 3 | -0.045 [-0.222, +0.120] | yes | no | no | no | no | no |
| adx | 1465 | -0.180 | -263.9 | [-554.5, +82.5] | 2 | -0.057 [-0.208, +0.082] | yes | no | no | no | no | no |
| vol | 1433 | -0.064 | -91.5 | [-380.9, +306.4] | 3 | +0.059 [-0.068, +0.207] | yes | no | no | no | no | no |
| volat | 1234 | -0.185 | -228.6 | [-450.4, +32.6] | 1 | -0.062 [-0.261, +0.125] | yes | no | no | no | no | no |
| cool24 | 1676 | -0.113 | -189.3 | [-540.9, +244.3] | 4 | +0.010 [-0.120, +0.133] | yes | no | no | no | no | no |
| streak3 | 2155 | -0.119 | -257.3 | [-651.0, +175.1] | 3 | +0.004 [-0.069, +0.074] | yes | no | no | no | no | no |
| fee3 | 1896 | -0.195 | -369.9 | [-656.3, -48.5] | 1 | -0.072 [-0.229, +0.046] | yes | no | no | no | no | no |
| pair:BTCUSDT | 645 | -0.187 | -120.7 | [-311.8, +86.3] | 2 | -0.064 [-0.271, +0.167] | yes | no | no | no | no | no |
| pair:ETHUSDT | 669 | -0.006 | -3.8 | [-149.2, +150.5] | 3 | +0.117 [-0.107, +0.316] | yes | no | no | no | no | no |
| pair:BNBUSDT | 662 | -0.151 | -100.1 | [-244.4, +60.1] | 2 | -0.028 [-0.177, +0.142] | yes | no | no | no | no | no |
| pair:XRPUSDT | 683 | -0.151 | -102.9 | [-286.8, +103.5] | 4 | -0.027 [-0.207, +0.183] | yes | no | no | no | no | no |
| pair:trailing_best | 680 | -0.152 | -103.1 | [-261.8, +53.5] | 3 | -0.029 [-0.239, +0.156] | yes | no | no | no | no | no |
| WALK_FORWARD_PICK | 977 | -0.189 | -185.1 | [-380.5, +2.3] | 2 | -0.066 [-0.279, +0.123] | yes | no | no | no | no | no |

Walk-forward picks (candidate with the best tune-window mean, per test window): htfD, htfD, vol, vol, pair:ETHUSDT, pair:ETHUSDT, pair:ETHUSDT, pair:ETHUSDT, pair:ETHUSDT

## donchian

| candidate | trades | mean net R | total net R | total R interval | windows > 0 (of 9) | mean vs unfiltered [interval] | P1 | P2 | P3 | P4 | edge survives (P1-P3) | passes all |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| none | 5296 | -0.174 | -921.5 | [-1208.3, -617.7] | 0 | +0.000 [+0.000, +0.000] | yes | no | no | no | no | no |
| htf4 | 2974 | -0.157 | -465.7 | [-666.1, -240.7] | 0 | +0.017 [-0.038, +0.067] | yes | no | no | no | no | no |
| cool24 | 4529 | -0.171 | -773.0 | [-1039.1, -484.7] | 0 | +0.003 [-0.017, +0.024] | yes | no | no | no | no | no |
| streak3 | 4809 | -0.171 | -822.8 | [-1082.5, -535.7] | 0 | +0.003 [-0.017, +0.020] | yes | no | no | no | no | no |
| fee3 | 3894 | -0.159 | -617.2 | [-832.5, -386.9] | 0 | +0.015 [-0.017, +0.053] | yes | no | no | no | no | no |
| pair:BTCUSDT | 1384 | -0.225 | -311.8 | [-418.3, -205.8] | 0 | -0.051 [-0.109, +0.004] | yes | no | no | no | no | no |
| pair:ETHUSDT | 1362 | -0.139 | -188.8 | [-301.8, -72.9] | 1 | +0.035 [-0.029, +0.103] | yes | no | no | no | no | no |
| pair:BNBUSDT | 1301 | -0.190 | -247.3 | [-364.4, -127.5] | 0 | -0.016 [-0.079, +0.048] | yes | no | no | no | no | no |
| pair:XRPUSDT | 1249 | -0.139 | -173.6 | [-288.5, -56.1] | 1 | +0.035 [-0.036, +0.106] | yes | no | no | no | no | no |
| pair:trailing_best | 1280 | -0.134 | -170.9 | [-272.0, -67.3] | 0 | +0.040 [-0.032, +0.113] | yes | no | no | no | no | no |
| WALK_FORWARD_PICK | 1453 | -0.183 | -265.7 | [-383.3, -143.2] | 0 | -0.009 [-0.072, +0.053] | yes | no | no | no | no | no |

Walk-forward picks (candidate with the best tune-window mean, per test window): htf4, pair:XRPUSDT, pair:XRPUSDT, pair:XRPUSDT, pair:XRPUSDT, pair:BTCUSDT, pair:BTCUSDT, pair:ETHUSDT, pair:ETHUSDT

## rsi

| candidate | trades | mean net R | total net R | total R interval | windows > 0 (of 9) | mean vs unfiltered [interval] | P1 | P2 | P3 | P4 | edge survives (P1-P3) | passes all |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| none | 624 | -0.285 | -178.1 | [-248.9, -112.9] | 0 | +0.000 [+0.000, +0.000] | yes | no | no | no | no | no |
| htfD | 252 | -0.293 | -73.7 | [-102.1, -43.3] | 0 | -0.007 [-0.095, +0.090] | no | no | no | no | no | no |
| streak3 | 619 | -0.292 | -180.9 | [-251.1, -114.7] | 0 | -0.007 [-0.017, +0.000] | yes | no | no | no | no | no |
| fee3 | 425 | -0.255 | -108.4 | [-156.0, -56.2] | 0 | +0.030 [-0.045, +0.094] | yes | no | no | no | no | no |
| pair:BTCUSDT | 147 | -0.459 | -67.4 | [-98.5, -39.8] | 0 | -0.173 [-0.319, -0.027] | no | no | no | no | no | no |
| pair:ETHUSDT | 161 | -0.188 | -30.2 | [-53.0, -7.5] | 1 | +0.098 [-0.024, +0.214] | no | no | no | no | no | no |
| pair:BNBUSDT | 159 | -0.266 | -42.2 | [-78.9, -9.5] | 0 | +0.020 [-0.103, +0.162] | no | no | no | no | no | no |
| pair:XRPUSDT | 157 | -0.244 | -38.2 | [-66.2, -12.9] | 2 | +0.042 [-0.112, +0.187] | no | no | no | no | no | no |
| pair:trailing_best | 145 | -0.321 | -46.5 | [-77.1, -19.1] | 0 | -0.035 [-0.165, +0.107] | no | no | no | no | no | no |
| WALK_FORWARD_PICK | 407 | -0.257 | -104.6 | [-150.7, -57.0] | 0 | +0.028 [-0.044, +0.092] | yes | no | no | no | no | no |

Walk-forward picks (candidate with the best tune-window mean, per test window): fee3, htfD, fee3, fee3, fee3, fee3, fee3, fee3, fee3

