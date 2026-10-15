"""Trading on the Quant Engine's decisions: the order book, the backtest and the nightly calibration.

- ``ledger``: a cash-and-positions book with the Vietnamese rules (lots of
  100, T+2, fees and sell tax, one-price limit days) and the order/exit rules
  shared by the paper account and the backtest.
- ``simulator``: replays the engine over cached history with given thresholds.
- ``factor``: the defensive trend book (the bot's default strategy); ``momentum``:
  the VN30 momentum rotation (a comparison book).
- ``calibrate``: each night, tries one-step changes of the tunable thresholds
  and keeps one only when it wins on recent data it was not tuned on.
"""
