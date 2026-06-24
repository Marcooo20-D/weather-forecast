/**
 * AURA Quant - Chart Manager Module
 * Handles TradingView Lightweight Charts (Price + EMA + RSI) & Chart.js Equity Curves
 * Exposes API as window.ChartManager
 */
(function() {
    let tvChart = null;
    let rsiChart = null;
    let candleSeries = null;
    let volumeSeries = null;
    let emaFastSeries = null;
    let emaSlowSeries = null;
    let rsiSeries = null;
    let equityChart = null;
    
    let obLine = null;
    let osLine = null;

    /**
     * Initializes TradingView Lightweight Charts
     */
    function initTVChart() {
        const chartElement = document.getElementById('priceChart');
        const rsiElement = document.getElementById('rsiChart');
        chartElement.innerHTML = "";
        rsiElement.innerHTML = "";

        if (typeof LightweightCharts === 'undefined') {
            chartElement.innerHTML = `
                <div style="display:flex; flex-direction:column; align-items:center; justify-content:center; height:100%; padding:20px; color:var(--text-muted); text-align:center; gap: 8px;">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:32px; height:32px; color:var(--color-warning);">
                        <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>
                        <line x1="12" y1="9" x2="12" y2="13"/>
                        <line x1="12" y1="17" x2="12.01" y2="17"/>
                    </svg>
                    <span style="font-weight:700; color:var(--text-primary);">Gagal Memuat TradingView Charts</span>
                    <span style="font-size:11px; max-width:240px;">Pustaka charting tidak dapat dimuat. Pastikan file lightweight-charts.js berada di folder yang benar.</span>
                </div>
            `;
            return;
        }

        try {
            // Price Chart
            tvChart = LightweightCharts.createChart(chartElement, {
                layout: {
                    background: { type: 'solid', color: '#090c14' },
                    textColor: '#768aa8',
                },
                grid: {
                    vertLines: { color: 'rgba(255, 255, 255, 0.01)' },
                    horzLines: { color: 'rgba(255, 255, 255, 0.01)' },
                },
                rightPriceScale: { borderColor: 'rgba(255, 255, 255, 0.03)' },
                timeScale: { borderColor: 'rgba(255, 255, 255, 0.03)' },
            });

            candleSeries = tvChart.addSeries(LightweightCharts.CandlestickSeries, {
                upColor: '#00ff88',
                downColor: '#ff3b30',
                borderVisible: false,
                wickUpColor: '#00ff88',
                wickDownColor: '#ff3b30',
            });

            // EMA Fast Series (Orange)
            emaFastSeries = tvChart.addSeries(LightweightCharts.LineSeries, {
                color: '#ff9f0a',
                lineWidth: 1.5,
                title: 'EMA Cepat',
                priceScaleId: 'right'
            });

            // EMA Slow Series (Blue)
            emaSlowSeries = tvChart.addSeries(LightweightCharts.LineSeries, {
                color: '#0a84ff',
                lineWidth: 1.5,
                title: 'EMA Lambat',
                priceScaleId: 'right'
            });

            volumeSeries = tvChart.addSeries(LightweightCharts.HistogramSeries, {
                color: '#26a69a',
                priceFormat: { type: 'volume' },
                priceScaleId: 'volume', 
            });

            volumeSeries.priceScale().applyOptions({
                scaleMargins: {
                    top: 0.8,
                    bottom: 0,
                },
            });

            // RSI Chart (Time linked)
            rsiChart = LightweightCharts.createChart(rsiElement, {
                layout: {
                    background: { type: 'solid', color: '#090c14' },
                    textColor: '#768aa8',
                },
                grid: {
                    vertLines: { color: 'rgba(255, 255, 255, 0.01)' },
                    horzLines: { color: 'rgba(255, 255, 255, 0.01)' },
                },
                rightPriceScale: { 
                    borderColor: 'rgba(255, 255, 255, 0.03)',
                    visible: true
                },
                timeScale: { 
                    borderColor: 'rgba(255, 255, 255, 0.03)',
                    visible: false // Stack neatly under priceChart
                },
            });

            rsiSeries = rsiChart.addSeries(LightweightCharts.LineSeries, {
                color: '#bf5af2', // Purple
                lineWidth: 1.5,
                title: 'RSI'
            });

            // Link timescales
            tvChart.timeScale().subscribeVisibleTimeRangeChange((range) => {
                if (range) rsiChart.timeScale().setVisibleTimeRange(range);
            });
            rsiChart.timeScale().subscribeVisibleTimeRangeChange((range) => {
                if (range) tvChart.timeScale().setVisibleTimeRange(range);
            });

            // Observers
            new ResizeObserver(entries => {
                if (entries.length === 0 || !tvChart) return;
                const { width, height } = entries[0].contentRect;
                tvChart.resize(width, height);
            }).observe(chartElement);

            new ResizeObserver(entries => {
                if (entries.length === 0 || !rsiChart) return;
                const { width, height } = entries[0].contentRect;
                rsiChart.resize(width, height);
            }).observe(rsiElement);

        } catch (e) {
            console.error("Failed to initialize LightweightCharts:", e);
        }
    }

    /**
     * Updates TV Chart series and markers
     */
    function updateTVChart(rawData, trades, indicators = {}) {
        if (!candleSeries || !volumeSeries || !emaFastSeries || !emaSlowSeries || !rsiSeries) return;

        const candleData = rawData.map(d => ({
            time: d.time,
            open: d.open,
            high: d.high,
            low: d.low,
            close: d.close
        }));

        const volumeData = rawData.map(d => ({
            time: d.time,
            value: d.volume,
            color: d.close >= d.open ? 'rgba(0, 255, 136, 0.15)' : 'rgba(255, 59, 48, 0.15)'
        }));

        candleSeries.setData(candleData);
        volumeSeries.setData(volumeData);

        // Update EMAs
        if (indicators.fastEma) {
            const fastEmaData = rawData.map((d, i) => {
                const val = indicators.fastEma[i];
                return val !== null && !isNaN(val) ? { time: d.time, value: val } : null;
            }).filter(item => item !== null);
            emaFastSeries.setData(fastEmaData);
        } else {
            emaFastSeries.setData([]);
        }

        if (indicators.slowEma) {
            const slowEmaData = rawData.map((d, i) => {
                const val = indicators.slowEma[i];
                return val !== null && !isNaN(val) ? { time: d.time, value: val } : null;
            }).filter(item => item !== null);
            emaSlowSeries.setData(slowEmaData);
        } else {
            emaSlowSeries.setData([]);
        }

        // Update RSI & Guide Lines
        if (indicators.rsi) {
            const rsiData = rawData.map((d, i) => {
                const val = indicators.rsi[i];
                return val !== null && !isNaN(val) ? { time: d.time, value: val } : null;
            }).filter(item => item !== null);
            rsiSeries.setData(rsiData);

            // Recreate limit lines
            if (obLine) rsiSeries.removePriceLine(obLine);
            if (osLine) rsiSeries.removePriceLine(osLine);

            obLine = rsiSeries.createPriceLine({
                price: parseFloat(indicators.rsiOverbought || 70),
                color: 'rgba(255, 59, 48, 0.4)',
                lineWidth: 1,
                lineStyle: LightweightCharts.LineStyle.Dashed,
                axisLabelVisible: true,
                title: 'OB'
            });

            osLine = rsiSeries.createPriceLine({
                price: parseFloat(indicators.rsiOversold || 40),
                color: 'rgba(0, 255, 136, 0.4)',
                lineWidth: 1,
                lineStyle: LightweightCharts.LineStyle.Dashed,
                axisLabelVisible: true,
                title: 'OS'
            });
        } else {
            rsiSeries.setData([]);
        }

        // Add visual entry/exit markers (LONG and SHORT support)
        const markers = [];
        trades.forEach(t => {
            const isLong = t.type === "LONG";
            
            if (t.entryTime) {
                markers.push({
                    time: t.entryTime,
                    position: isLong ? 'belowBar' : 'aboveBar',
                    color: isLong ? '#00ff88' : '#ff9f0a',
                    shape: isLong ? 'arrowUp' : 'arrowDown',
                    text: isLong ? 'ENTRY BUY' : 'ENTRY SHORT'
                });
            }

            if (t.exitTime) {
                const isProfit = t.pnlUsd >= 0;
                markers.push({
                    time: t.exitTime,
                    position: isLong ? 'aboveBar' : 'belowBar',
                    color: isProfit ? '#00ff88' : '#ff3b30',
                    shape: isLong ? 'arrowDown' : 'arrowUp',
                    text: isLong 
                        ? (isProfit ? 'EXIT LONG (TP/TS)' : 'EXIT LONG (SL)') 
                        : (isProfit ? 'COVER SHORT (TP/TS)' : 'COVER SHORT (SL)')
                });
            }
        });

        // Filter duplicates
        const seen = new Set();
        const unique = [];
        markers.forEach(m => {
            const key = `${m.time}_${m.shape}`;
            if (!seen.has(key)) {
                seen.add(key);
                unique.push(m);
            }
        });

        unique.sort((a, b) => a.time - b.time);
        candleSeries.setMarkers(unique);
        tvChart.timeScale().fitContent();
        
        // Match scales
        setTimeout(() => {
            const range = tvChart.timeScale().getVisibleLogicalRange();
            if (range) rsiChart.timeScale().setVisibleLogicalRange(range);
        }, 50);
    }

    /**
     * Renders equity curve growth Chart.js chart
     */
    function renderEquityChart(equityHistory) {
        const dates = equityHistory.map(e => e.date);
        const strategyEquity = equityHistory.map(e => e.strategy);
        const bhEquity = equityHistory.map(e => e.buyAndHold);

        if (equityChart) {
            equityChart.destroy();
        }

        const ctxEquity = document.getElementById('equityChart').getContext('2d');
        equityChart = new Chart(ctxEquity, {
            type: 'line',
            data: {
                labels: dates,
                datasets: [
                    {
                        label: 'Garis Uang Anda',
                        data: strategyEquity,
                        borderColor: '#00ff88',
                        backgroundColor: 'rgba(0, 255, 136, 0.02)',
                        borderWidth: 2,
                        pointRadius: 0,
                        tension: 0.1,
                        fill: true
                    },
                    {
                        label: 'Didiamkan Saja (Buy & Hold)',
                        data: bhEquity,
                        borderColor: 'rgba(255, 255, 255, 0.15)',
                        borderWidth: 1.5,
                        borderDash: [5, 5],
                        pointRadius: 0,
                        tension: 0.1
                    }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                scales: {
                    x: { grid: { color: 'rgba(255, 255, 255, 0.01)' }, ticks: { color: '#5f6f8f', maxTicksLimit: 6 } },
                    y: { grid: { color: 'rgba(255, 255, 255, 0.01)' }, ticks: { color: '#5f6f8f', callback: val => '$' + val.toLocaleString() } }
                },
                plugins: {
                    legend: {
                        display: true,
                        labels: { color: '#768aa8', font: { family: 'Plus Jakarta Sans', weight: 'bold' } }
                    }
                }
            }
        });
    }

    // Expose to window
    window.ChartManager = {
        initChart: initTVChart,
        updateChart: updateTVChart,
        renderEquity: renderEquityChart,
        get candleSeries() { return candleSeries; },
        get volumeSeries() { return volumeSeries; }
    };
})();
