/**
 * AURA Quant - Technical Indicators & Volatility Module
 * Exposes math indicators as window.IndicatorEngine
 */
(function() {
    /**
     * Calculates Exponential Moving Average (EMA)
     */
    function calculateEMA(prices, period) {
        const ema = new Array(prices.length).fill(null);
        if (prices.length < period) return ema;

        // Simple SMA for first point
        let sum = 0;
        for (let i = 0; i < period; i++) sum += prices[i];
        let currentEma = sum / period;
        ema[period - 1] = currentEma;

        const k = 2 / (period + 1);
        for (let i = period; i < prices.length; i++) {
            currentEma = prices[i] * k + currentEma * (1 - k);
            ema[i] = currentEma;
        }
        return ema;
    }

    /**
     * Calculates Relative Strength Index (RSI)
     */
    function calculateRSI(prices, period) {
        const rsi = new Array(prices.length).fill(null);
        if (prices.length <= period) return rsi;

        let gains = 0, losses = 0;
        for (let i = 1; i <= period; i++) {
            const diff = prices[i] - prices[i - 1];
            if (diff > 0) gains += diff;
            else losses -= diff;
        }

        let avgGain = gains / period;
        let avgLoss = losses / period;
        rsi[period] = avgLoss === 0 ? 100 : 100 - (100 / (1 + (avgGain / avgLoss)));

        for (let i = period + 1; i < prices.length; i++) {
            const diff = prices[i] - prices[i - 1];
            const currentGain = diff > 0 ? diff : 0;
            const currentLoss = diff < 0 ? -diff : 0;

            avgGain = (avgGain * (period - 1) + currentGain) / period;
            avgLoss = (avgLoss * (period - 1) + currentLoss) / period;

            rsi[i] = avgLoss === 0 ? 100 : 100 - (100 / (1 + (avgGain / avgLoss)));
        }
        return rsi;
    }

    /**
     * Calculates Average True Range (ATR) representing market volatility
     */
    function calculateATR(data, period = 14) {
        const atr = new Array(data.length).fill(null);
        if (data.length <= period) return atr;

        const tr = new Array(data.length).fill(0);
        
        // First True Range
        tr[0] = data[0].high - data[0].low;
        for (let i = 1; i < data.length; i++) {
            const h = data[i].high;
            const l = data[i].low;
            const prevC = data[i - 1].close;
            tr[i] = Math.max(h - l, Math.abs(h - prevC), Math.abs(l - prevC));
        }

        // First ATR (SMA of True Range)
        let sumTR = 0;
        for (let i = 0; i < period; i++) sumTR += tr[i];
        
        let currentAtr = sumTR / period;
        atr[period - 1] = currentAtr;

        // Wilder smoothing
        for (let i = period; i < data.length; i++) {
            currentAtr = (currentAtr * (period - 1) + tr[i]) / period;
            atr[i] = currentAtr;
        }
        return atr;
    }

    // Expose to window
    window.IndicatorEngine = {
        ema: calculateEMA,
        rsi: calculateRSI,
        atr: calculateATR
    };
})();
