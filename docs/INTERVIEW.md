"So tell me about this project."

It's a trading bot for Polymarket's 5-minute Bitcoin markets — every five minutes, the market asks "will BTC close higher or lower?" and pays $1 per share if you're right. Our bot has one move: in the final 45 seconds, when Bitcoin lurches past the strike price on Coinbase, it buys the winning side before the market's quotes catch up — a few hundred milliseconds of stale prices, harvested with fill-or-kill orders. Simple idea. Everything hard about this project was learning what was actually true about it.

"What did you learn?"

One — the market already knows. We tested over 150 signals — order flow, book imbalance, momentum, cross-exchange gaps. Every single one was already priced in. Our fills win almost exactly as often as their price implies. The lesson: in a liquid market, your edge is never cleverness, it's an asymmetry — being faster, having data others don't, or having capital where others can't. Everything else is a story you tell yourself.

Two — your backtest will lie to you in a specific way. Our simulator said +10¢ per share. It was simulating a bot that could catch every opportunity instantly. Real physics: at our 440ms round-trip, only 21% of stale prices still exist when the order lands — and we later measured that exactly. The rule we now live by: only realized fills count. A backtest is a ceiling, never a promise.

Three — your instruments can lie worse than the market does. For a week we thought the bot was losing 4¢ a share. It was breaking even — the ledger was booking every fill at the worst allowed price instead of the actual one. Meanwhile our paper simulator was pretending orders took 125ms when reality was 436ms. We nearly killed a breakeven strategy and nearly trusted a fantasy one. Fix your measurements before you judge your strategy.

Four — variance wears a costume. The "amazing week" that felt like a hot strategy? Mostly a different strategy riding a favorable volatility regime — one that had already formally failed its statistical test mid-week. And the "haywire" days later were three coin flips landing wrong. Daily P&L is weather. We now only trust one number: equal-weight cents-per-share over eight clean days, with a t-statistic.

Five — constraints choose your strategy for you. At a $135 bankroll, the exchange's $1 minimum order silently deleted every cheap, high-payoff trade and left only expensive favorites — the exact subset with no edge. Nobody changed the code; the bankroll changed the strategy. We found the knee at $400 and validated there.

Six — discipline is a system, not a mood. Kill bars that are never relaxed to pass. A validation epoch that resets whenever anything meaningful changes. A freeze while measuring. An automated nightly verdict so no human mood gets a vote. The July 4th launch failed because a good-looking number was trusted early; everything since exists so that can't happen again.

"Where does it stand?"

It now lives on a $0/month server in Stockholm — three times closer to the exchange, calibrated with real measured orders, self-healing, self-pruning, honest at every layer — running an eight-day trial of the one configuration all the evidence supports. If it clears +2¢ a share, it goes live and compounds. If it doesn't, it will have proven that with fake money instead of ours — which is the entire point.

"One sentence?"

We spent a week making a bot smarter, and the whole payoff came from making it honest — because you can't improve what you're measuring wrong, and the market punishes self-deception faster than any other mistake.