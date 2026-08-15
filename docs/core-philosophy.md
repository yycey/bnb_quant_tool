# Core Philosophy

## One sentence

Use AI to turn fragmented crypto market information into actionable, risk-aware, continuously improving trading decisions.

## What this tool is

This project is an AI-driven crypto decision system focused on BNB.

It is designed to answer four questions on every analysis cycle:

1. What is the most likely market direction right now?
2. Is the signal strong enough to trade?
3. If yes, how should the trade be executed?
4. After the trade, what should the system learn?

## What this tool is not

- Not a pure indicator dashboard
- Not a simple LLM wrapper that outputs bullish or bearish comments
- Not a strategy toy that ignores execution and risk
- Not a static ruleset that never learns from mistakes

## Decision chain

The intended decision chain is:

1. Collect market data
   - price
   - volume
   - technical indicators
   - multi-timeframe structure

2. Collect context
   - news
   - market sentiment
   - on-chain signals
   - macro context
   - BNB-specific event factors

3. Run intelligence layers
   - LLM market reasoning
   - institutional strategy voting
   - deep learning / temporal pattern recognition
   - historical learning context injection

4. Apply quality control
   - confidence threshold
   - risk/reward threshold
   - event conflict checks
   - regime conflict checks
   - adverse performance guardrails

5. Produce execution output
   - direction
   - entry zone
   - stop loss
   - take profit ladder
   - position sizing
   - invalidation rules

6. Learn from outcome
   - paper trading result
   - review summary
   - parameter updates
   - pattern memory
   - capability cards

## Design principles

### 1. AI must sit at the center

AI should shape the decision, not just narrate it.

### 2. Every output must be executable

A useful answer is not "looks bullish".
A useful answer is "LONG only if entry, stop, risk/reward, and risk gate all pass".

### 3. WAIT is a feature, not a failure

When evidence is weak or conflicting, the correct output is to not trade.

### 4. Risk control outranks prediction confidence

Even a strong directional view should be blocked when risk conditions are bad.

### 5. Learning must change future behavior

Review data is only valuable if it affects future thresholds, weights, and decisions.

### 6. Explainability matters

Users should see:

- what the system decided
- why it decided that
- what blocked execution if it refused to trade
- what evidence mattered most

## Product standard

Any new feature should strengthen at least one of these:

- better market understanding
- better AI judgment
- better execution quality
- better risk control
- better learning feedback
- better explainability

If a feature does not improve one of those six, it probably does not belong in the core product.
