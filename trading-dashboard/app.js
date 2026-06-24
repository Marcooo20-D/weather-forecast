/**
 * AURA Quant Terminal - Orchestrator Main Entry
 * Coordinates: config.js, IndicatorEngine.js, BacktestEngine.js, WebsocketClient.js, ChartManager.js, ui.js
 */
document.addEventListener("DOMContentLoaded", function() {
    // DOM Controls
    const assetSelect = document.getElementById("asset-select");
    const profileSelect = document.getElementById("profile-select");
    const initialCapitalInput = document.getElementById("initial-capital");
    const timeframeSelect = document.getElementById("timeframe-select");
    const orderSizeInput = document.getElementById("order-size-input");
    const whipsawToggle = document.getElementById("whipsaw-filter-toggle");
    
    const runBtn = document.getElementById("run-btn");
    const btnText = runBtn.querySelector(".btn-text");
    const loader = runBtn.querySelector(".loader");
    
    // Header ticking widgets
    const statusDot = document.getElementById("status-dot");
    const statusLbl = document.getElementById("status-lbl");
    const liveToggle = document.getElementById("live-toggle");
    const liveDemoPanel = document.getElementById("live-demo-panel");
    const simulateSignalBtn = document.getElementById("simulate-signal-btn");

    // Simple Signal Target fields
    const activeAssetLbl = document.getElementById("active-asset-lbl");
    const activeActionText = document.getElementById("active-action-text");
    const activeActionBox = document.getElementById("active-action-box");
    const targetEntryPrice = document.getElementById("target-entry-price");
    const targetTpPrice = document.getElementById("target-tp-price");
    const targetSlPrice = document.getElementById("target-sl-price");

    // Metrics widgets
    const summaryProfitPct = document.getElementById("summary-profit-pct");
    const summaryTextDesc = document.getElementById("summary-text-desc");
    const metricBalance = document.getElementById("metric-balance");
    const metricInitialRef = document.getElementById("metric-initial-ref");
    const metricWinrate = document.getElementById("metric-winrate");
    const metricTradesWonCount = document.getElementById("metric-trades-won-count");
    const metricDrawdown = document.getElementById("metric-drawdown");
    const metricDrawdownDesc = document.getElementById("metric-drawdown-desc");
    const timelineBody = document.getElementById("timeline-body");

    // New metrics widgets
    const metricProfitFactor = document.getElementById("metric-profit-factor");
    const metricSharpeRatio = document.getElementById("metric-sharpe-ratio");
    const metricRiskOfRuin = document.getElementById("metric-risk-of-ruin");

    // Custom parameter sliders DOM references
    const customParamsContainer = document.getElementById("custom-params-container");
    const inputEmaFast = document.getElementById("input-ema-fast");
    const inputEmaSlow = document.getElementById("input-ema-slow");
    const inputRsiLen = document.getElementById("input-rsi-len");
    const inputRsiOs = document.getElementById("input-rsi-os");
    const inputRsiOb = document.getElementById("input-rsi-ob");
    const inputTp = document.getElementById("input-tp");
    const inputTs = document.getElementById("input-ts");
    const inputAlloc = document.getElementById("input-alloc");

    const valEmaFast = document.getElementById("val-ema-fast");
    const valEmaSlow = document.getElementById("val-ema-slow");
    const valRsiLen = document.getElementById("val-rsi-len");
    const valAlloc = document.getElementById("val-alloc");
    
    // Auto-Optimizer Button
    const optimizeBtn = document.getElementById("optimize-btn");

    // State Variables
    let isLiveStreaming = false;
    let activeHistoricalData = [];
    let lastTradeCount = 0;
    let activeTradesList = [];

    // --- 1. State Persistence & UI Loader ---
    function updateSliderLabels() {
        valEmaFast.textContent = inputEmaFast.value;
        valEmaSlow.textContent = inputEmaSlow.value;
        valRsiLen.textContent = inputRsiLen.value;
        valAlloc.textContent = inputAlloc.value;
    }

    function toggleCustomContainer() {
        if (profileSelect.value === "custom") {
            customParamsContainer.classList.remove("hidden");
        } else {
            customParamsContainer.classList.add("hidden");
        }
    }

    function initializeState() {
        const saved = window.AuraUI.loadState();
        initialCapitalInput.value = saved.capital;
        assetSelect.value = saved.asset;
        profileSelect.value = saved.profile;
        timeframeSelect.value = saved.timeframe || "86400";
        orderSizeInput.value = saved.orderSize || "1000";
        whipsawToggle.checked = saved.whipsawFilter !== false;

        // Restore custom overrides
        inputEmaFast.value = localStorage.getItem("aura_cust_ema_fast") || "12";
        inputEmaSlow.value = localStorage.getItem("aura_cust_ema_slow") || "26";
        inputRsiLen.value = localStorage.getItem("aura_cust_rsi_len") || "14";
        inputRsiOs.value = localStorage.getItem("aura_cust_rsi_os") || "40";
        inputRsiOb.value = localStorage.getItem("aura_cust_rsi_ob") || "70";
        inputTp.value = localStorage.getItem("aura_cust_tp") || "8";
        inputTs.value = localStorage.getItem("aura_cust_ts") || "2.5";
        inputAlloc.value = localStorage.getItem("aura_cust_alloc") || "65";

        updateSliderLabels();
        toggleCustomContainer();
    }

    // --- 2. Main Simulation Coordinator ---
    async function executeSimulation() {
        const asset = assetSelect.value;
        const profile = profileSelect.value;
        const capital = parseFloat(initialCapitalInput.value);
        const timeframe = parseInt(timeframeSelect.value);
        const orderSize = parseFloat(orderSizeInput.value);
        const whipsawFilter = whipsawToggle.checked;

        // Save selection state locally
        window.AuraUI.saveState(capital, asset, profile, timeframe, orderSize, whipsawFilter);

        // Save custom slider settings locally
        localStorage.setItem("aura_cust_ema_fast", inputEmaFast.value);
        localStorage.setItem("aura_cust_ema_slow", inputEmaSlow.value);
        localStorage.setItem("aura_cust_rsi_len", inputRsiLen.value);
        localStorage.setItem("aura_cust_rsi_os", inputRsiOs.value);
        localStorage.setItem("aura_cust_rsi_ob", inputRsiOb.value);
        localStorage.setItem("aura_cust_tp", inputTp.value);
        localStorage.setItem("aura_cust_ts", inputTs.value);
        localStorage.setItem("aura_cust_alloc", inputAlloc.value);

        // Map asset selection to Coinbase product (e.g. BTC -> BTC-USD)
        const productId = window.AuraConfig.products[asset] || "BTC-USD";

        try {
            // A. Fetch historical data dynamically
            const rawData = await window.AuraUI.fetchData(productId, timeframe);
            activeHistoricalData = [...rawData];

            // B. Prepare backtester options
            const backtestOptions = {
                orderSize: orderSize,
                whipsawFilter: whipsawFilter,
                emaFast: inputEmaFast.value,
                emaSlow: inputEmaSlow.value,
                rsiLen: inputRsiLen.value,
                rsiOs: inputRsiOs.value,
                rsiOb: inputRsiOb.value,
                tp: inputTp.value,
                ts: inputTs.value,
                alloc: inputAlloc.value
            };

            // C. Run backtester simulation
            const results = window.BacktestEngine.run(activeHistoricalData, profile, capital, backtestOptions);
            lastTradeCount = results.trades.length;
            activeTradesList = results.trades;

            // D. Render metrics
            const totalCount = results.trades.length;
            const winCount = results.trades.filter(t => t.pnlUsd > 0).length;
            const winRate = totalCount > 0 ? (winCount / totalCount) * 100 : 0;
            const roiPct = ((results.finalBalance - capital) / capital) * 100;

            summaryProfitPct.textContent = `${roiPct >= 0 ? '+' : ''}${roiPct.toFixed(2)}%`;
            summaryProfitPct.className = `stat-val ${roiPct >= 0 ? 'positive' : 'negative'}`;

            // Describe results in conversational Indonesian
            summaryTextDesc.innerHTML = `Menggunakan <strong>${results.config.name}</strong>, sistem menganalisis harga riil pasar (${timeframeSelect.options[timeframeSelect.selectedIndex].text.split(' ')[0]}) dan melakukan <strong>${totalCount} kali Entry & Exit</strong> otomatis (Long & Short). Hasilnya <strong>${winCount} kali untung</strong> dengan ukuran order <strong>$${orderSize.toLocaleString()}</strong>, mengubah modal awal Anda menjadi <strong>$${results.finalBalance.toLocaleString(undefined, { maximumFractionDigits: 2 })}</strong>.`;

            // Update widgets
            metricBalance.textContent = `$${results.finalBalance.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
            metricInitialRef.textContent = `Uang Awal: $${capital.toLocaleString()}`;

            metricWinrate.textContent = `${winRate.toFixed(1)}%`;
            metricTradesWonCount.textContent = `${winCount} dari ${totalCount} transaksi untung`;

            // Max Drawdown
            let maxDd = 0;
            let peak = capital;
            results.equityHistory.forEach(eq => {
                if (eq.strategy > peak) peak = eq.strategy;
                const dd = ((peak - eq.strategy) / peak) * 100;
                if (dd > maxDd) maxDd = dd;
            });
            metricDrawdown.textContent = `-${maxDd.toFixed(2)}%`;
            
            const riskLabel = maxDd < 10 ? "Risiko Rendah" : maxDd < 22 ? "Risiko Seimbang" : "Risiko Tinggi (Agresif)";
            metricDrawdownDesc.textContent = `Skor Risiko: ${riskLabel}`;

            // Advanced stats
            metricProfitFactor.textContent = results.stats.profitFactor.toFixed(2);
            metricProfitFactor.className = `metric-value ${results.stats.profitFactor >= 1.5 ? 'positive' : results.stats.profitFactor >= 1.0 ? 'positive' : 'negative'}`;

            metricSharpeRatio.textContent = results.stats.sharpeRatio.toFixed(2);
            metricSharpeRatio.className = `metric-value ${results.stats.sharpeRatio >= 1.0 ? 'positive' : results.stats.sharpeRatio >= 0 ? 'positive' : 'negative'}`;

            metricRiskOfRuin.textContent = `${results.stats.riskOfRuin.toFixed(1)}%`;
            metricRiskOfRuin.className = `metric-value ${results.stats.riskOfRuin > 20 ? 'negative' : 'positive'}`;
            document.getElementById("metric-ror-desc").textContent = results.stats.riskOfRuin > 50 ? "Sangat Berbahaya (High Ruin)" : results.stats.riskOfRuin > 20 ? "Risiko Sedang" : "Sangat Aman";

            // E. Populate timeline logs
            populateSimpleTimeline(results.trades);

            // F. Sync active signal card instructions
            activeAssetLbl.textContent = `${asset} / USD (${timeframeSelect.options[timeframeSelect.selectedIndex].text.split(' ')[0]})`;
            const lastCandle = activeHistoricalData[activeHistoricalData.length - 1];

            if (results.isFinalOpen) {
                const isLong = results.trades[results.trades.length - 1].type === "LONG";
                activeActionBox.className = `signal-status-box ${isLong ? 'border-glow-green' : 'border-glow-orange'}`;
                activeActionText.textContent = isLong ? "LONG ACTIVE (HOLD)" : "SHORT ACTIVE (HOLD)";
                activeActionText.style.color = isLong ? "var(--color-success)" : "var(--color-primary)";
                activeActionText.style.textShadow = isLong ? "0 0 10px var(--color-success-glow)" : "0 0 10px var(--color-primary-glow)";

                targetEntryPrice.textContent = `$${results.activeEntryPrice.toLocaleString()}`;
                targetTpPrice.textContent = `$${(isLong ? results.activeEntryPrice * (1 + results.config.takeProfitPct) : results.activeEntryPrice * (1 - results.config.takeProfitPct)).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
                targetSlPrice.textContent = `$${(isLong ? results.activeEntryPrice * (1 - results.config.trailingStopPct) : results.activeEntryPrice * (1 + results.config.trailingStopPct)).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
            } else {
                activeActionBox.className = "signal-status-box border-glow-yellow";
                activeActionText.textContent = "READY FOR SENSOR";
                activeActionText.style.color = "var(--color-warning)";
                activeActionText.style.textShadow = "0 0 10px var(--color-warning-glow)";

                targetEntryPrice.textContent = `$${lastCandle.close.toLocaleString()}`;
                targetTpPrice.textContent = `$${(lastCandle.close * (1 + results.config.takeProfitPct)).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
                targetSlPrice.textContent = `$${(lastCandle.close * (1 - results.config.trailingStopPct)).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
            }

            const modelSignal = await window.AuraUI.fetchSignal(productId);
            if (modelSignal && modelSignal.available) {
                const isLongModel = modelSignal.signal === "LONG";
                activeAssetLbl.textContent = `${modelSignal.symbol} / MODEL (${modelSignal.horizon_bars}H)`;
                activeActionBox.className = `signal-status-box ${isLongModel ? 'border-glow-green' : 'border-glow-orange'}`;
                activeActionText.textContent = `${modelSignal.signal} ${modelSignal.risk && modelSignal.risk.can_trade ? 'PAPER SIGNAL' : 'WATCHLIST'}`;
                activeActionText.style.color = isLongModel ? "var(--color-success)" : "var(--color-primary)";
                activeActionText.style.textShadow = isLongModel ? "0 0 10px var(--color-success-glow)" : "0 0 10px var(--color-primary-glow)";

                targetEntryPrice.textContent = `$${modelSignal.entry_price.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
                targetTpPrice.textContent = `$${modelSignal.take_profit.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
                targetSlPrice.textContent = `$${modelSignal.stop_loss.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
            }

            // G. Render TV charts & heatmap
            window.ChartManager.updateChart(activeHistoricalData, results.trades, {
                fastEma: results.fastEma,
                slowEma: results.slowEma,
                rsi: results.rsi,
                rsiOverbought: results.config.rsiOverbought,
                rsiOversold: results.config.rsiOversold
            });
            window.ChartManager.renderEquity(results.equityHistory);
            window.AuraUI.renderHeatmap(results.equityHistory, capital);

        } catch (e) {
            console.error("Backtest simulation failed:", e);
            window.AuraUI.showToast("Gagal Memuat Data", "Terjadi kesalahan koneksi atau pembatasan API dari bursa.", "danger");
        }
    }

    function populateSimpleTimeline(trades) {
        timelineBody.innerHTML = "";
        if (trades.length === 0) {
            timelineBody.innerHTML = `<div class="timeline-item"><div class="timeline-content"><span class="timeline-title">Belum ada transaksi tereksekusi.</span></div></div>`;
            return;
        }

        const list = [...trades].reverse();
        list.forEach(t => {
            const item = document.createElement("div");
            item.className = "timeline-item";
            
            const isProfit = t.pnlUsd >= 0;
            const dotClass = isProfit ? "success" : "danger";
            const badgeClass = isProfit ? "success" : "danger";
            const prefix = isProfit ? "+" : "";

            item.innerHTML = `
                <div class="timeline-dot ${dotClass}"></div>
                <div class="timeline-content">
                    <div>
                        <div class="timeline-title">Transaksi #${t.id}: ${t.reason} (${t.type})</div>
                        <div class="timeline-desc">Masuk: ${t.entryDate} ($${t.entryPrice.toLocaleString()}) | Keluar: ${t.exitDate} ($${t.exitPrice.toLocaleString()})</div>
                    </div>
                    <span class="timeline-badge-net ${badgeClass}">${prefix}${t.pnlPercent.toFixed(2)}% (${prefix}$${t.pnlUsd.toLocaleString(undefined, { maximumFractionDigits: 0 })})</span>
                </div>
            `;
            timelineBody.appendChild(item);
        });
    }

    // --- 3. Live WebSocket & HTTP Polling Setup ---
    function startLiveStream() {
        const asset = assetSelect.value;
        const productId = window.AuraConfig.products[asset] || "BTC-USD";
        const profile = profileSelect.value;
        
        let params;
        if (profile === "custom") {
            params = {
                takeProfitPct: parseFloat(inputTp.value) / 100,
                trailingStopPct: parseFloat(inputTs.value) / 100
            };
        } else {
            params = window.AuraConfig.profiles[profile];
        }

        statusDot.className = "status-indicator connecting";
        statusLbl.textContent = "Connecting Live...";
        liveDemoPanel.classList.remove("hidden");

        // Subscription callbacks
        window.WebsocketClient.connect(
            productId,
            // Dynamic Tick handler
            function(tick) {
                if (!activeHistoricalData || activeHistoricalData.length === 0) return;

                const granularity = parseInt(timeframeSelect.value);
                const candleTime = Math.floor(tick.time / granularity) * granularity;
                const lastCandle = activeHistoricalData[activeHistoricalData.length - 1];

                // A. Sync tick to active historical candles array
                if (lastCandle.time === candleTime) {
                    lastCandle.close = tick.price;
                    if (tick.price > lastCandle.high) lastCandle.high = tick.price;
                    if (tick.price < lastCandle.low) lastCandle.low = tick.price;
                    lastCandle.volume = tick.volume;
                } else if (candleTime > lastCandle.time) {
                    activeHistoricalData.push({
                        time: candleTime,
                        date: tick.date,
                        open: tick.price,
                        high: tick.price,
                        low: tick.price,
                        close: tick.price,
                        volume: tick.volume
                    });
                    if (activeHistoricalData.length > 700) activeHistoricalData.shift();
                }

                // B. Re-run backtest simulation in real-time on live tick
                const capital = parseFloat(initialCapitalInput.value);
                const orderSize = parseFloat(orderSizeInput.value);
                const whipsawFilter = whipsawToggle.checked;

                const backtestOptions = {
                    orderSize: orderSize,
                    whipsawFilter: whipsawFilter,
                    emaFast: inputEmaFast.value,
                    emaSlow: inputEmaSlow.value,
                    rsiLen: inputRsiLen.value,
                    rsiOs: inputRsiOs.value,
                    rsiOb: inputRsiOb.value,
                    tp: inputTp.value,
                    ts: inputTs.value,
                    alloc: inputAlloc.value
                };

                const results = window.BacktestEngine.run(activeHistoricalData, profile, capital, backtestOptions);
                activeTradesList = results.trades;

                // C. Detect newly triggered signals live
                if (results.trades.length > lastTradeCount) {
                    const newTrade = results.trades[results.trades.length - 1];
                    const isProfit = newTrade.pnlUsd >= 0;
                    if (newTrade.reason.includes("Ambil Untung") || newTrade.reason.includes("Batasi Rugi") || newTrade.reason.includes("Keluar")) {
                        window.AuraUI.showToast(
                            isProfit ? "💰 TP / EXIT COVERS" : "⚠️ SL / TRAILING STOP TRIPPED",
                            `Posisi ${newTrade.type} ditutup di harga $${newTrade.exitPrice.toLocaleString()}. Net: ${isProfit ? '+' : ''}$${newTrade.pnlUsd.toLocaleString()}`,
                            isProfit ? "success" : "danger"
                        );
                    } else {
                        window.AuraUI.showToast(
                            "🔔 EKSEKUSI SINYAL BARU",
                            `Entry posisi ${newTrade.type} dibuka pada harga $${newTrade.entryPrice.toLocaleString()}.`,
                            "info"
                        );
                    }
                    lastTradeCount = results.trades.length;
                }

                // D. Update UI widgets
                const totalCount = results.trades.length;
                const winCount = results.trades.filter(t => t.pnlUsd > 0).length;
                const winRate = totalCount > 0 ? (winCount / totalCount) * 100 : 0;
                const roiPct = ((results.finalBalance - capital) / capital) * 100;

                summaryProfitPct.textContent = `${roiPct >= 0 ? '+' : ''}${roiPct.toFixed(2)}%`;
                summaryProfitPct.className = `stat-val ${roiPct >= 0 ? 'positive' : 'negative'}`;

                metricBalance.textContent = `$${results.finalBalance.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
                metricWinrate.textContent = `${winRate.toFixed(1)}%`;
                metricTradesWonCount.textContent = `${winCount} dari ${totalCount} transaksi untung`;

                // Update active target instructions card
                activeAssetLbl.textContent = `${assetSelect.value} / USD (LIVE)`;
                if (results.isFinalOpen) {
                    const isLong = results.trades[results.trades.length - 1].type === "LONG";
                    activeActionBox.className = `signal-status-box ${isLong ? 'border-glow-green' : 'border-glow-orange'}`;
                    activeActionText.textContent = isLong ? "LONG ACTIVE (HOLD)" : "SHORT ACTIVE (HOLD)";
                    activeActionText.style.color = isLong ? "var(--color-success)" : "var(--color-primary)";
                    activeActionText.style.textShadow = isLong ? "0 0 10px var(--color-success-glow)" : "0 0 10px var(--color-primary-glow)";

                    targetEntryPrice.textContent = `$${results.activeEntryPrice.toLocaleString()}`;
                    targetTpPrice.textContent = `$${(isLong ? results.activeEntryPrice * (1 + results.config.takeProfitPct) : results.activeEntryPrice * (1 - results.config.takeProfitPct)).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
                    targetSlPrice.textContent = `$${(isLong ? results.activeEntryPrice * (1 - results.config.trailingStopPct) : results.activeEntryPrice * (1 + results.config.trailingStopPct)).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
                } else {
                    activeActionBox.className = "signal-status-box border-glow-yellow";
                    activeActionText.textContent = "READY FOR SIGNAL";
                    activeActionText.style.color = "var(--color-warning)";
                    activeActionText.style.textShadow = "0 0 10px var(--color-warning-glow)";

                    targetEntryPrice.textContent = `$${tick.price.toLocaleString()}`;
                    targetTpPrice.textContent = `$${(tick.price * (1 + results.config.takeProfitPct)).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
                    targetSlPrice.textContent = `$${(tick.price * (1 - results.config.trailingStopPct)).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
                }

                // Update dynamic chart indicator lines (EMA fast/slow + RSI) on live ticks
                window.ChartManager.updateChart(activeHistoricalData, results.trades, {
                    fastEma: results.fastEma,
                    slowEma: results.slowEma,
                    rsi: results.rsi,
                    rsiOverbought: results.config.rsiOverbought,
                    rsiOversold: results.config.rsiOversold
                });

                // Update pricing header indicators
                statusDot.className = "status-indicator";
                statusDot.style.backgroundColor = "var(--color-primary)";
                statusDot.style.boxShadow = "0 0 10px var(--color-primary-glow)";
                statusDot.style.backgroundImage = "none";
                statusLbl.innerHTML = `LIVE: <span style="font-family:'JetBrains Mono'; font-weight:bold; color:var(--color-primary); font-size:12px;">$${tick.price.toLocaleString()}</span>`;
            },
            // Connect handler
            function() {
                window.AuraUI.showToast("Koneksi Live Aktif!", `Berlangganan real-time ticker ${productId}.`, "success");
            },
            // Disconnect handler
            function() {
                console.log("WebSocket Disconnected.");
            }
        );
    }

    function stopLiveStream() {
        window.WebsocketClient.disconnect();
        
        liveDemoPanel.classList.add("hidden");
        statusDot.className = "status-indicator";
        statusDot.style.backgroundColor = "var(--color-success)";
        statusDot.style.boxShadow = "0 0 8px var(--color-success-glow)";
        statusLbl.textContent = "Engine Ready";

        // Restore backtesting analytics
        executeSimulation();
    }

    // --- 4. Interactive Toast Alerts Simulator ---
    const simulatedSignals = [
        {
            title: "🔔 SINYAL SHORT TERDETEKSI",
            desc: "Aksi: <strong>ENTRY SHORT</strong> pada koin <strong>SOL</strong> di harga <strong>$143.20</strong>.<br>Target (TP): $131.74 | Stop (SL): $146.78",
            type: "info"
        },
        {
            title: "💰 TARGET TERCAPAI (EXIT SHORT)",
            desc: "Aksi: <strong>COVER SHORT</strong> untuk <strong>SOL</strong> pada harga target <strong>$131.74</strong>.<br>Hasil bersih: <strong>Untung +8.0% (+$800)</strong>.",
            type: "success"
        },
        {
            title: "🔔 SINYAL LONG TERDETEKSI",
            desc: "Aksi: <strong>ENTRY BUY (LONG)</strong> pada koin <strong>BTC</strong> di harga <strong>$64,800</strong>.<br>Target (TP): $70,000 | Stop (SL): $63,180",
            type: "info"
        },
        {
            title: "⚠️ PENGAMAN TERTIMPA (EXIT LONG)",
            desc: "Aksi: <strong>EXIT LONG</strong> untuk <strong>BTC</strong> akibat koreksi ke harga <strong>$63,180</strong>.<br>Hasil bersih: <strong>Rugi -2.5% (-$250)</strong>.",
            type: "danger"
        }
    ];

    let currentSignalIndex = 0;
    simulateSignalBtn.addEventListener("click", function() {
        const sig = simulatedSignals[currentSignalIndex];
        window.AuraUI.showToast(sig.title, sig.desc, sig.type);
        currentSignalIndex = (currentSignalIndex + 1) % simulatedSignals.length;
    });

    // --- 5. Controls Bindings ---
    liveToggle.addEventListener("change", function() {
        isLiveStreaming = liveToggle.checked;
        if (isLiveStreaming) {
            runBtn.disabled = true;
            startLiveStream();
        } else {
            runBtn.disabled = false;
            stopLiveStream();
        }
    });

    assetSelect.addEventListener("change", function() {
        if (isLiveStreaming) {
            startLiveStream();
        } else {
            executeSimulation();
        }
    });

    profileSelect.addEventListener("change", function() {
        toggleCustomContainer();
        if (!isLiveStreaming) {
            executeSimulation();
        } else {
            let params;
            if (profileSelect.value === "custom") {
                params = {
                    takeProfitPct: parseFloat(inputTp.value) / 100,
                    trailingStopPct: parseFloat(inputTs.value) / 100
                };
            } else {
                params = window.AuraConfig.profiles[profileSelect.value];
            }
            const currentLiveEntry = parseFloat(targetEntryPrice.textContent.replace('$', '').replace(/,/g, ''));
            if (currentLiveEntry > 0) {
                targetTpPrice.textContent = `$${(currentLiveEntry * (1 + params.takeProfitPct)).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
                targetSlPrice.textContent = `$${(currentLiveEntry * (1 - params.trailingStopPct)).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
            }
        }
    });

    // Bind real-time input sliders for dynamic recalculations
    [inputEmaFast, inputEmaSlow, inputRsiLen, inputAlloc].forEach(slider => {
        slider.addEventListener("input", function() {
            updateSliderLabels();
            executeSimulation();
        });
    });

    [inputRsiOs, inputRsiOb, inputTp, inputTs].forEach(input => {
        input.addEventListener("input", function() {
            executeSimulation();
        });
    });

    // Strategy Auto-Optimizer binding
    optimizeBtn.addEventListener("click", function() {
        if (activeHistoricalData.length < 50) {
            window.AuraUI.showToast("Gagal Optimasi", "Data historis tidak mencukupi.", "danger");
            return;
        }

        optimizeBtn.disabled = true;
        const optText = optimizeBtn.querySelector(".opt-btn-text");
        const prevText = optText.textContent;
        optText.textContent = "MENGOPTIMASI PARAMETER STRATEGI...";

        // Delay slightly to allow button text render update
        setTimeout(() => {
            try {
                const capital = parseFloat(initialCapitalInput.value);
                const best = window.BacktestEngine.optimize(activeHistoricalData, capital);
                
                if (best) {
                    // Update input values
                    inputEmaFast.value = best.emaFast;
                    inputEmaSlow.value = best.emaSlow;
                    inputRsiLen.value = best.rsiLen;
                    inputRsiOs.value = best.rsiOs;
                    inputRsiOb.value = best.rsiOb;
                    inputTp.value = best.tp;
                    inputTs.value = best.ts;
                    inputAlloc.value = best.alloc;

                    // Update UI labels and run backtester
                    updateSliderLabels();
                    executeSimulation();

                    window.AuraUI.showToast(
                        "🎉 Optimasi Sukses!",
                        `Parameter Terbaik: EMA Cepat ${best.emaFast}, Lambat ${best.emaSlow}, RSI ${best.rsiLen}. ROI: +${best.roi.toFixed(2)}%`,
                        "success"
                    );
                } else {
                    window.AuraUI.showToast("Gagal Optimasi", "Tidak ditemukan kombinasi parameter yang menguntungkan.", "warning");
                }
            } catch (err) {
                console.error("Parameter optimization failed:", err);
                window.AuraUI.showToast("Error Optimasi", "Gagal memproses perhitungan optimasi kuantitatif.", "danger");
            } finally {
                optimizeBtn.disabled = false;
                optText.textContent = prevText;
            }
        }, 100);
    });

    timeframeSelect.addEventListener("change", function() {
        if (!isLiveStreaming) {
            executeSimulation();
        }
    });

    orderSizeInput.addEventListener("change", function() {
        if (!isLiveStreaming) {
            executeSimulation();
        }
    });

    whipsawToggle.addEventListener("change", function() {
        if (!isLiveStreaming) {
            executeSimulation();
        }
    });

    runBtn.addEventListener("click", async function() {
        if (!isLiveStreaming) {
            runBtn.disabled = true;
            btnText.textContent = "MENGANALISIS DATA PASAR...";
            loader.classList.remove("hidden");

            try {
                await executeSimulation();
                window.AuraUI.showToast("Simulasi Sukses!", "Metrik dan data pasar berhasil dimutakhirkan.", "success");
            } catch (err) {
                window.AuraUI.showToast("Gagal Memproses", "Kesalahan saat melakukan perhitungan kuantitatif.", "danger");
            } finally {
                runBtn.disabled = false;
                btnText.textContent = "UJI PERFORMA SISTEM";
                loader.classList.add("hidden");
            }
        }
    });

    // --- CSV Export Handler ---
    const exportCsvBtn = document.getElementById("export-csv-btn");
    if (exportCsvBtn) {
        exportCsvBtn.addEventListener("click", function() {
            if (!activeTradesList || activeTradesList.length === 0) {
                window.AuraUI.showToast("Ekspor Gagal", "Belum ada transaksi tereksekusi untuk diekspor.", "warning");
                return;
            }
            
            // Construct CSV header & rows
            const headers = ["ID Transaksi", "Tipe Posisi", "Alasan Keluar", "Tanggal Masuk", "Harga Masuk ($)", "Tanggal Keluar", "Harga Keluar ($)", "PnL ($)", "PnL (%)"];
            const rows = activeTradesList.map(t => [
                t.id,
                t.type,
                t.reason,
                t.entryDate,
                t.entryPrice.toFixed(2),
                t.exitDate,
                t.exitPrice.toFixed(2),
                t.pnlUsd.toFixed(2),
                t.pnlPercent.toFixed(2)
            ]);
            
            // Generate CSV content
            const csvContent = [headers.join(",")].concat(rows.map(r => r.map(val => `"${val}"`).join(","))).join("\n");
            
            // Trigger browser download
            const blob = new Blob([csvContent], { type: "text/csv;charset=utf-8;" });
            const url = URL.createObjectURL(blob);
            const link = document.createElement("a");
            const assetName = assetSelect.value || "CRYPTO";
            link.setAttribute("href", url);
            link.setAttribute("download", `aura_quant_trades_${assetName.toLowerCase()}.csv`);
            link.style.visibility = "hidden";
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
            
            window.AuraUI.showToast("Ekspor Sukses", `Riwayat transaksi berhasil disimpan ke file CSV.`, "success");
        });
    }

    // --- 6. Boot App Engine ---
    initializeState();
    window.ChartManager.initChart();
    
    // Delay execution slightly to ensure charts render layout sizes correctly
    setTimeout(executeSimulation, 100);
});
