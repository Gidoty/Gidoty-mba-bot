# Cost sensitivity

Plan hash `915faca89a86f2ed`. The same trades as the cost split, re-priced. Average net R per FILLED trade [95% interval over trades]; fee schedule and BNB discount UNVERIFIED (see the module). Slippage 5.5 bps per side on every market fill; maker entries have none and exits stay market orders.

## ema - A: 57 months (2020-04 to 2024-12)

| scenario | filled trades | avg net R [95% CI] |
|---|---|---|
| S0 baseline: taker both sides, regular tier | 2725 | +0.058 [-0.069, +0.196] |
| S1 pay fees in BNB (-10%) | 2725 | +0.064 [-0.063, +0.202] |
| S2 VIP 1 taker | 2725 | +0.070 [-0.057, +0.208] |
| S3 VIP 2 taker | 2725 | +0.076 [-0.051, +0.214] |
| S4 VIP 3 taker | 2725 | +0.080 [-0.047, +0.218] |
| S5 VIP 3 and BNB | 2725 | +0.083 [-0.044, +0.222] |
| S6 maker entry, regular tier | miss 0% random | 2725 | +0.109 [-0.018, +0.248] |
| S6 maker entry, regular tier | miss 20% random | 2180 | +0.109 [-0.018, +0.248] |
| S6 maker entry, regular tier | miss 20% adverse | 2180 | -0.877 [-0.890, -0.864] |
| S6 maker entry, regular tier | miss 40% random | 1635 | +0.109 [-0.018, +0.248] |
| S6 maker entry, regular tier | miss 40% adverse | 1635 | -1.043 [-1.046, -1.040] |
| S7 maker entry and BNB | miss 0% random | 2725 | +0.113 [-0.014, +0.252] |
| S7 maker entry and BNB | miss 20% random | 2180 | +0.113 [-0.014, +0.252] |
| S7 maker entry and BNB | miss 20% adverse | 2180 | -0.873 [-0.886, -0.860] |
| S7 maker entry and BNB | miss 40% random | 1635 | +0.113 [-0.014, +0.252] |
| S7 maker entry and BNB | miss 40% adverse | 1635 | -1.039 [-1.041, -1.036] |
| S8 maker entry, VIP 3 and BNB | miss 0% random | 2725 | +0.127 [-0.000, +0.266] |
| S8 maker entry, VIP 3 and BNB | miss 20% random | 2180 | +0.127 [-0.000, +0.266] |
| S8 maker entry, VIP 3 and BNB | miss 20% adverse | 2180 | -0.859 [-0.872, -0.845] |
| S8 maker entry, VIP 3 and BNB | miss 40% random | 1635 | +0.127 [-0.000, +0.266] |
| S8 maker entry, VIP 3 and BNB | miss 40% adverse | 1635 | -1.024 [-1.026, -1.021] |
| zero cost (gross, funding kept) | 2725 | +0.184 |

## ema - B: 10 months (2025-01 to 2025-10)

| scenario | filled trades | avg net R [95% CI] |
|---|---|---|
| S0 baseline: taker both sides, regular tier | 493 | -0.300 [-0.467, -0.115] |
| S1 pay fees in BNB (-10%) | 493 | -0.294 [-0.460, -0.108] |
| S2 VIP 1 taker | 493 | -0.287 [-0.453, -0.102] |
| S3 VIP 2 taker | 493 | -0.280 [-0.447, -0.095] |
| S4 VIP 3 taker | 493 | -0.276 [-0.443, -0.091] |
| S5 VIP 3 and BNB | 493 | -0.272 [-0.439, -0.087] |
| S6 maker entry, regular tier | miss 0% random | 493 | -0.244 [-0.411, -0.059] |
| S6 maker entry, regular tier | miss 20% random | 394 | -0.244 [-0.411, -0.059] |
| S6 maker entry, regular tier | miss 20% adverse | 394 | -0.949 [-0.971, -0.926] |
| S6 maker entry, regular tier | miss 40% random | 296 | -0.244 [-0.411, -0.059] |
| S6 maker entry, regular tier | miss 40% adverse | 296 | -1.044 [-1.047, -1.042] |
| S7 maker entry and BNB | miss 0% random | 493 | -0.239 [-0.406, -0.054] |
| S7 maker entry and BNB | miss 20% random | 394 | -0.239 [-0.406, -0.054] |
| S7 maker entry and BNB | miss 20% adverse | 394 | -0.945 [-0.966, -0.922] |
| S7 maker entry and BNB | miss 40% random | 296 | -0.239 [-0.406, -0.054] |
| S7 maker entry and BNB | miss 40% adverse | 296 | -1.040 [-1.042, -1.037] |
| S8 maker entry, VIP 3 and BNB | miss 0% random | 493 | -0.223 [-0.391, -0.039] |
| S8 maker entry, VIP 3 and BNB | miss 20% random | 394 | -0.223 [-0.391, -0.039] |
| S8 maker entry, VIP 3 and BNB | miss 20% adverse | 394 | -0.929 [-0.950, -0.906] |
| S8 maker entry, VIP 3 and BNB | miss 40% random | 296 | -0.223 [-0.391, -0.039] |
| S8 maker entry, VIP 3 and BNB | miss 40% adverse | 296 | -1.025 [-1.027, -1.023] |
| zero cost (gross, funding kept) | 493 | -0.160 |

## ema - C: the nine test windows of #102 (2022-04 to 2026-09)

| scenario | filled trades | avg net R [95% CI] |
|---|---|---|
| S0 baseline: taker both sides, regular tier | 2659 | -0.123 [-0.227, -0.008] |
| S1 pay fees in BNB (-10%) | 2659 | -0.116 [-0.220, -0.001] |
| S2 VIP 1 taker | 2659 | -0.109 [-0.213, +0.006] |
| S3 VIP 2 taker | 2659 | -0.102 [-0.206, +0.013] |
| S4 VIP 3 taker | 2659 | -0.098 [-0.202, +0.017] |
| S5 VIP 3 and BNB | 2659 | -0.094 [-0.198, +0.021] |
| S6 maker entry, regular tier | miss 0% random | 2659 | -0.064 [-0.168, +0.050] |
| S6 maker entry, regular tier | miss 20% random | 2127 | -0.064 [-0.168, +0.050] |
| S6 maker entry, regular tier | miss 20% adverse | 2127 | -0.921 [-0.932, -0.909] |
| S6 maker entry, regular tier | miss 40% random | 1595 | -0.064 [-0.168, +0.050] |
| S6 maker entry, regular tier | miss 40% adverse | 1595 | -1.050 [-1.051, -1.048] |
| S7 maker entry and BNB | miss 0% random | 2659 | -0.059 [-0.164, +0.055] |
| S7 maker entry and BNB | miss 20% random | 2127 | -0.059 [-0.164, +0.055] |
| S7 maker entry and BNB | miss 20% adverse | 2127 | -0.916 [-0.927, -0.904] |
| S7 maker entry and BNB | miss 40% random | 1595 | -0.059 [-0.164, +0.055] |
| S7 maker entry and BNB | miss 40% adverse | 1595 | -1.045 [-1.046, -1.043] |
| S8 maker entry, VIP 3 and BNB | miss 0% random | 2659 | -0.043 [-0.147, +0.071] |
| S8 maker entry, VIP 3 and BNB | miss 20% random | 2127 | -0.043 [-0.147, +0.071] |
| S8 maker entry, VIP 3 and BNB | miss 20% adverse | 2127 | -0.900 [-0.911, -0.888] |
| S8 maker entry, VIP 3 and BNB | miss 40% random | 1595 | -0.043 [-0.147, +0.071] |
| S8 maker entry, VIP 3 and BNB | miss 40% adverse | 1595 | -1.028 [-1.030, -1.027] |
| zero cost (gross, funding kept) | 2659 | +0.023 |

## donchian - A: 57 months (2020-04 to 2024-12)

| scenario | filled trades | avg net R [95% CI] |
|---|---|---|
| S0 baseline: taker both sides, regular tier | 5550 | -0.173 [-0.204, -0.139] |
| S1 pay fees in BNB (-10%) | 5550 | -0.165 [-0.197, -0.131] |
| S2 VIP 1 taker | 5550 | -0.157 [-0.189, -0.123] |
| S3 VIP 2 taker | 5550 | -0.149 [-0.181, -0.115] |
| S4 VIP 3 taker | 5550 | -0.145 [-0.177, -0.110] |
| S5 VIP 3 and BNB | 5550 | -0.140 [-0.172, -0.105] |
| S6 maker entry, regular tier | miss 0% random | 5550 | -0.106 [-0.138, -0.072] |
| S6 maker entry, regular tier | miss 20% random | 4440 | -0.106 [-0.138, -0.072] |
| S6 maker entry, regular tier | miss 20% adverse | 4440 | -0.610 [-0.624, -0.596] |
| S6 maker entry, regular tier | miss 40% random | 3330 | -0.106 [-0.138, -0.072] |
| S6 maker entry, regular tier | miss 40% adverse | 3330 | -0.847 [-0.856, -0.839] |
| S7 maker entry and BNB | miss 0% random | 5550 | -0.101 [-0.133, -0.066] |
| S7 maker entry and BNB | miss 20% random | 4440 | -0.101 [-0.133, -0.066] |
| S7 maker entry and BNB | miss 20% adverse | 4440 | -0.604 [-0.618, -0.591] |
| S7 maker entry and BNB | miss 40% random | 3330 | -0.101 [-0.133, -0.066] |
| S7 maker entry and BNB | miss 40% adverse | 3330 | -0.842 [-0.851, -0.834] |
| S8 maker entry, VIP 3 and BNB | miss 0% random | 5550 | -0.082 [-0.114, -0.048] |
| S8 maker entry, VIP 3 and BNB | miss 20% random | 4440 | -0.082 [-0.114, -0.048] |
| S8 maker entry, VIP 3 and BNB | miss 20% adverse | 4440 | -0.586 [-0.600, -0.573] |
| S8 maker entry, VIP 3 and BNB | miss 40% random | 3330 | -0.082 [-0.114, -0.048] |
| S8 maker entry, VIP 3 and BNB | miss 40% adverse | 3330 | -0.824 [-0.832, -0.816] |
| zero cost (gross, funding kept) | 5550 | -0.008 |

## donchian - B: 10 months (2025-01 to 2025-10)

| scenario | filled trades | avg net R [95% CI] |
|---|---|---|
| S0 baseline: taker both sides, regular tier | 1012 | -0.166 [-0.248, -0.076] |
| S1 pay fees in BNB (-10%) | 1012 | -0.157 [-0.239, -0.067] |
| S2 VIP 1 taker | 1012 | -0.147 [-0.230, -0.058] |
| S3 VIP 2 taker | 1012 | -0.138 [-0.221, -0.049] |
| S4 VIP 3 taker | 1012 | -0.132 [-0.215, -0.043] |
| S5 VIP 3 and BNB | 1012 | -0.126 [-0.209, -0.037] |
| S6 maker entry, regular tier | miss 0% random | 1012 | -0.086 [-0.169, +0.003] |
| S6 maker entry, regular tier | miss 20% random | 810 | -0.086 [-0.169, +0.003] |
| S6 maker entry, regular tier | miss 20% adverse | 810 | -0.615 [-0.650, -0.580] |
| S6 maker entry, regular tier | miss 40% random | 607 | -0.086 [-0.169, +0.003] |
| S6 maker entry, regular tier | miss 40% adverse | 607 | -0.868 [-0.888, -0.847] |
| S7 maker entry and BNB | miss 0% random | 1012 | -0.080 [-0.162, +0.010] |
| S7 maker entry and BNB | miss 20% random | 810 | -0.080 [-0.162, +0.010] |
| S7 maker entry and BNB | miss 20% adverse | 810 | -0.609 [-0.643, -0.574] |
| S7 maker entry and BNB | miss 40% random | 607 | -0.080 [-0.162, +0.010] |
| S7 maker entry and BNB | miss 40% adverse | 607 | -0.861 [-0.882, -0.841] |
| S8 maker entry, VIP 3 and BNB | miss 0% random | 1012 | -0.058 [-0.141, +0.032] |
| S8 maker entry, VIP 3 and BNB | miss 20% random | 810 | -0.058 [-0.141, +0.032] |
| S8 maker entry, VIP 3 and BNB | miss 20% adverse | 810 | -0.587 [-0.621, -0.552] |
| S8 maker entry, VIP 3 and BNB | miss 40% random | 607 | -0.058 [-0.141, +0.032] |
| S8 maker entry, VIP 3 and BNB | miss 40% adverse | 607 | -0.840 [-0.860, -0.820] |
| zero cost (gross, funding kept) | 1012 | +0.030 |

## donchian - C: the nine test windows of #102 (2022-04 to 2026-09)

| scenario | filled trades | avg net R [95% CI] |
|---|---|---|
| S0 baseline: taker both sides, regular tier | 5296 | -0.174 [-0.212, -0.137] |
| S1 pay fees in BNB (-10%) | 5296 | -0.165 [-0.202, -0.128] |
| S2 VIP 1 taker | 5296 | -0.155 [-0.193, -0.118] |
| S3 VIP 2 taker | 5296 | -0.146 [-0.183, -0.109] |
| S4 VIP 3 taker | 5296 | -0.140 [-0.178, -0.103] |
| S5 VIP 3 and BNB | 5296 | -0.134 [-0.171, -0.097] |
| S6 maker entry, regular tier | miss 0% random | 5296 | -0.093 [-0.131, -0.057] |
| S6 maker entry, regular tier | miss 20% random | 4237 | -0.093 [-0.131, -0.057] |
| S6 maker entry, regular tier | miss 20% adverse | 4237 | -0.619 [-0.633, -0.605] |
| S6 maker entry, regular tier | miss 40% random | 3178 | -0.093 [-0.131, -0.057] |
| S6 maker entry, regular tier | miss 40% adverse | 3178 | -0.863 [-0.872, -0.854] |
| S7 maker entry and BNB | miss 0% random | 5296 | -0.087 [-0.124, -0.050] |
| S7 maker entry and BNB | miss 20% random | 4237 | -0.087 [-0.124, -0.050] |
| S7 maker entry and BNB | miss 20% adverse | 4237 | -0.612 [-0.627, -0.598] |
| S7 maker entry and BNB | miss 40% random | 3178 | -0.087 [-0.124, -0.050] |
| S7 maker entry and BNB | miss 40% adverse | 3178 | -0.857 [-0.865, -0.848] |
| S8 maker entry, VIP 3 and BNB | miss 0% random | 5296 | -0.064 [-0.102, -0.028] |
| S8 maker entry, VIP 3 and BNB | miss 20% random | 4237 | -0.064 [-0.102, -0.028] |
| S8 maker entry, VIP 3 and BNB | miss 20% adverse | 4237 | -0.590 [-0.604, -0.576] |
| S8 maker entry, VIP 3 and BNB | miss 40% random | 3178 | -0.064 [-0.102, -0.028] |
| S8 maker entry, VIP 3 and BNB | miss 40% adverse | 3178 | -0.835 [-0.843, -0.826] |
| zero cost (gross, funding kept) | 5296 | +0.025 |

## rsi - A: 57 months (2020-04 to 2024-12)

| scenario | filled trades | avg net R [95% CI] |
|---|---|---|
| S0 baseline: taker both sides, regular tier | 655 | -0.266 [-0.331, -0.201] |
| S1 pay fees in BNB (-10%) | 655 | -0.258 [-0.323, -0.193] |
| S2 VIP 1 taker | 655 | -0.251 [-0.315, -0.185] |
| S3 VIP 2 taker | 655 | -0.243 [-0.308, -0.178] |
| S4 VIP 3 taker | 655 | -0.239 [-0.303, -0.174] |
| S5 VIP 3 and BNB | 655 | -0.234 [-0.298, -0.169] |
| S6 maker entry, regular tier | miss 0% random | 655 | -0.201 [-0.265, -0.137] |
| S6 maker entry, regular tier | miss 20% random | 524 | -0.201 [-0.265, -0.137] |
| S6 maker entry, regular tier | miss 20% adverse | 524 | -0.400 [-0.468, -0.330] |
| S6 maker entry, regular tier | miss 40% random | 393 | -0.201 [-0.265, -0.137] |
| S6 maker entry, regular tier | miss 40% adverse | 393 | -0.743 [-0.806, -0.675] |
| S7 maker entry and BNB | miss 0% random | 655 | -0.196 [-0.260, -0.132] |
| S7 maker entry and BNB | miss 20% random | 524 | -0.196 [-0.260, -0.132] |
| S7 maker entry and BNB | miss 20% adverse | 524 | -0.395 [-0.464, -0.325] |
| S7 maker entry and BNB | miss 40% random | 393 | -0.196 [-0.260, -0.132] |
| S7 maker entry and BNB | miss 40% adverse | 393 | -0.738 [-0.801, -0.670] |
| S8 maker entry, VIP 3 and BNB | miss 0% random | 655 | -0.178 [-0.242, -0.114] |
| S8 maker entry, VIP 3 and BNB | miss 20% random | 524 | -0.178 [-0.242, -0.114] |
| S8 maker entry, VIP 3 and BNB | miss 20% adverse | 524 | -0.379 [-0.447, -0.309] |
| S8 maker entry, VIP 3 and BNB | miss 40% random | 393 | -0.178 [-0.242, -0.114] |
| S8 maker entry, VIP 3 and BNB | miss 40% adverse | 393 | -0.721 [-0.784, -0.654] |
| zero cost (gross, funding kept) | 655 | -0.106 |

## rsi - B: 10 months (2025-01 to 2025-10)

| scenario | filled trades | avg net R [95% CI] |
|---|---|---|
| S0 baseline: taker both sides, regular tier | 113 | -0.215 [-0.371, -0.066] |
| S1 pay fees in BNB (-10%) | 113 | -0.206 [-0.361, -0.057] |
| S2 VIP 1 taker | 113 | -0.197 [-0.352, -0.048] |
| S3 VIP 2 taker | 113 | -0.188 [-0.342, -0.039] |
| S4 VIP 3 taker | 113 | -0.183 [-0.337, -0.034] |
| S5 VIP 3 and BNB | 113 | -0.177 [-0.331, -0.028] |
| S6 maker entry, regular tier | miss 0% random | 113 | -0.138 [-0.291, +0.009] |
| S6 maker entry, regular tier | miss 20% random | 90 | -0.138 [-0.291, +0.009] |
| S6 maker entry, regular tier | miss 20% adverse | 90 | -0.320 [-0.485, -0.153] |
| S6 maker entry, regular tier | miss 40% random | 68 | -0.138 [-0.291, +0.009] |
| S6 maker entry, regular tier | miss 40% adverse | 68 | -0.623 [-0.800, -0.447] |
| S7 maker entry and BNB | miss 0% random | 113 | -0.131 [-0.285, +0.016] |
| S7 maker entry and BNB | miss 20% random | 90 | -0.131 [-0.285, +0.016] |
| S7 maker entry and BNB | miss 20% adverse | 90 | -0.314 [-0.479, -0.148] |
| S7 maker entry and BNB | miss 40% random | 68 | -0.131 [-0.285, +0.016] |
| S7 maker entry and BNB | miss 40% adverse | 68 | -0.617 [-0.794, -0.442] |
| S8 maker entry, VIP 3 and BNB | miss 0% random | 113 | -0.110 [-0.263, +0.037] |
| S8 maker entry, VIP 3 and BNB | miss 20% random | 90 | -0.110 [-0.263, +0.037] |
| S8 maker entry, VIP 3 and BNB | miss 20% adverse | 90 | -0.296 [-0.459, -0.130] |
| S8 maker entry, VIP 3 and BNB | miss 40% random | 68 | -0.110 [-0.263, +0.037] |
| S8 maker entry, VIP 3 and BNB | miss 40% adverse | 68 | -0.598 [-0.774, -0.423] |
| zero cost (gross, funding kept) | 113 | -0.024 |

## rsi - C: the nine test windows of #102 (2022-04 to 2026-09)

| scenario | filled trades | avg net R [95% CI] |
|---|---|---|
| S0 baseline: taker both sides, regular tier | 624 | -0.285 [-0.352, -0.221] |
| S1 pay fees in BNB (-10%) | 624 | -0.276 [-0.343, -0.212] |
| S2 VIP 1 taker | 624 | -0.267 [-0.333, -0.203] |
| S3 VIP 2 taker | 624 | -0.257 [-0.324, -0.193] |
| S4 VIP 3 taker | 624 | -0.252 [-0.318, -0.188] |
| S5 VIP 3 and BNB | 624 | -0.246 [-0.312, -0.182] |
| S6 maker entry, regular tier | miss 0% random | 624 | -0.206 [-0.272, -0.141] |
| S6 maker entry, regular tier | miss 20% random | 499 | -0.206 [-0.272, -0.141] |
| S6 maker entry, regular tier | miss 20% adverse | 499 | -0.400 [-0.467, -0.331] |
| S6 maker entry, regular tier | miss 40% random | 374 | -0.206 [-0.272, -0.141] |
| S6 maker entry, regular tier | miss 40% adverse | 374 | -0.740 [-0.803, -0.668] |
| S7 maker entry and BNB | miss 0% random | 624 | -0.199 [-0.266, -0.135] |
| S7 maker entry and BNB | miss 20% random | 499 | -0.199 [-0.266, -0.135] |
| S7 maker entry and BNB | miss 20% adverse | 499 | -0.394 [-0.461, -0.325] |
| S7 maker entry and BNB | miss 40% random | 374 | -0.199 [-0.266, -0.135] |
| S7 maker entry and BNB | miss 40% adverse | 374 | -0.734 [-0.797, -0.662] |
| S8 maker entry, VIP 3 and BNB | miss 0% random | 624 | -0.177 [-0.244, -0.112] |
| S8 maker entry, VIP 3 and BNB | miss 20% random | 499 | -0.177 [-0.244, -0.112] |
| S8 maker entry, VIP 3 and BNB | miss 20% adverse | 499 | -0.375 [-0.442, -0.306] |
| S8 maker entry, VIP 3 and BNB | miss 40% random | 374 | -0.177 [-0.244, -0.112] |
| S8 maker entry, VIP 3 and BNB | miss 40% adverse | 374 | -0.714 [-0.777, -0.642] |
| zero cost (gross, funding kept) | 624 | -0.088 |

