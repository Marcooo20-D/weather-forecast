/**
 * AURA Quant - Backtesting Engine Simulation Module
 * Exposes simulation logic as window.BacktestEngine
 */
(function() {
    /**
     * Executes backtest on price data using selected risk profile settings
     */
    function runBacktestSimulation(data, profileKey, initialCapital, options = {}) {
        let config;
        
        // Handle custom profile values directly
        if (profileKey === "custom") {
            config = {
                name: "Gaya Kustom",
                fastPeriod: parseInt(options.emaFast || 12),
                slowPeriod: parseInt(options.emaSlow || 26),
                rsiLength: parseInt(options.rsiLen || 14),
                rsiOversold: parseInt(options.rsiOs || 40),
                rsiOverbought: parseInt(options.rsiOb || 70),
                takeProfitPct: parseFloat(options.tp || 8) / 100,
                trailingStopPct: parseFloat(options.ts || 2.5) / 100,
                allocationPct: parseFloat(options.alloc || 65) / 100
            };
        } else {
            config = window.AuraConfig.profiles[profileKey] || window.AuraConfig.profiles.moderate;
        }

        const baseFee = window.AuraConfig.defaultFeePercent;
        const baseSlippage = window.AuraConfig.defaultSlippagePercent;

        // Extract options
        const orderSize = parseFloat(options.orderSize || 1000);
        const whipsawFilterEnabled = options.whipsawFilter !== false;

        const dataLength = data.length;
        if (dataLength < 50) {
            throw new Error("Data historis tidak mencukupi untuk melakukan analisis.");
        }

        // 1. Calculate Technical Indicators using window.IndicatorEngine
        const closes = data.map(d => d.close);
        const fastEma = window.IndicatorEngine.ema(closes, config.fastPeriod);
        const slowEma = window.IndicatorEngine.ema(closes, config.slowPeriod);
        const rsi = window.IndicatorEngine.rsi(closes, config.rsiLength);
        const atr = window.IndicatorEngine.atr(data, 14);

        // 2. Generate Signals (Golden cross = BUY/LONG, Death cross = SELL/SHORT)
        const buySignals = new Array(dataLength).fill(false);
        const sellSignals = new Array(dataLength).fill(false);

        for (let i = config.slowPeriod + 1; i < dataLength; i++) {
            const prevFast = fastEma[i - 1];
            const prevSlow = slowEma[i - 1];
            const currFast = fastEma[i];
            const currSlow = slowEma[i];

            if (prevFast <= prevSlow && currFast > currSlow) {
                const currentRsi = rsi[i] || 50;
                if (currentRsi < config.rsiOverbought) {
                    // Whipsaw / Low Volatility filter
                    let passFilter = true;
                    if (whipsawFilterEnabled && atr[i]) {
                        const volRatio = atr[i] / closes[i];
                        if (volRatio < 0.015) {
                            passFilter = false;
                        }
                    }
                    if (passFilter) {
                        buySignals[i] = true;
                    }
                }
            } else if (prevFast >= prevSlow && currFast < currSlow) {
                const currentRsi = rsi[i] || 50;
                if (currentRsi > config.rsiOversold) {
                    // Whipsaw / Low Volatility filter
                    let passFilter = true;
                    if (whipsawFilterEnabled && atr[i]) {
                        const volRatio = atr[i] / closes[i];
                        if (volRatio < 0.015) {
                            passFilter = false;
                        }
                    }
                    if (passFilter) {
                        sellSignals[i] = true;
                    }
                }
            }
        }

        // 3. Execution Simulation Loop
        let balance = initialCapital;
        let position = 0;
        let positionType = null; // "LONG" or "SHORT"
        let entryPrice = 0;
        let entryDate = "";
        let entryTime = 0;
        let peakPrice = 0;
        let stopPrice = 0;
        let takeProfitPrice = 0;
        let costBasis = 0;

        const trades = [];
        const equityHistory = [];
        const executedBuys = new Array(dataLength).fill(false);

        // Daily carry charge rate for perp futures (0.015% per day)
        const dailyFundingRate = 0.00015;

        for (let i = 0; i < dataLength; i++) {
            const today = data[i];
            const price = today.close;
            const high = today.high;
            const low = today.low;
            const open = today.open;

            let todayEquity = balance;

            // Calculate Volatility factor for Dynamic Slippage (using ATR)
            const currentAtr = atr[i] || (price * 0.02);
            const volatilityRatio = currentAtr / price;
            const orderImpactFactor = 1 + (orderSize / 250000);
            const dynamicSlippage = baseSlippage * (1 + volatilityRatio * 40) * orderImpactFactor;

            // 3.1 Position checks & funding fees (Exit management)
            if (position > 0) {
                // Deduct dynamic funding fee (holding cost)
                const fundingFee = (position * price) * dailyFundingRate;
                balance -= fundingFee;
                
                if (positionType === "LONG") {
                    todayEquity = position * price + balance;

                    // Trailing Stop for Long
                    if (price > peakPrice) {
                        peakPrice = price;
                        stopPrice = peakPrice * (1 - config.trailingStopPct);
                    }

                    let exitPrice = 0;
                    let exitReason = "";
                    let exitTimeSec = today.time;

                    if (high >= takeProfitPrice) {
                        exitPrice = takeProfitPrice;
                        exitReason = "Ambil Untung (Take Profit)";
                    } else if (low <= stopPrice) {
                        exitPrice = Math.min(open, stopPrice) * (1 - dynamicSlippage);
                        exitReason = "Batasi Rugi (Trailing Stop)";
                    } else if (sellSignals[i]) {
                        exitPrice = price * (1 - dynamicSlippage);
                        exitReason = "Sinyal Keluar Tren (Sell)";
                    }

                    if (exitPrice > 0) {
                        const gross = position * exitPrice;
                        const fee = gross * baseFee;
                        const net = gross - fee;

                        const profitUSD = net - costBasis;
                        const profitPct = (profitUSD / costBasis) * 100;

                        trades.push({
                            id: trades.length + 1,
                            entryDate: entryDate,
                            entryTime: entryTime,
                            exitDate: today.date,
                            exitTime: exitTimeSec,
                            type: "LONG",
                            entryPrice: parseFloat(entryPrice.toFixed(2)),
                            exitPrice: parseFloat(exitPrice.toFixed(2)),
                            pnlUsd: parseFloat(profitUSD.toFixed(2)),
                            pnlPercent: parseFloat(profitPct.toFixed(2)),
                            reason: exitReason
                        });

                        balance += net;
                        position = 0;
                        positionType = null;
                        costBasis = 0;
                        todayEquity = balance;
                    }
                } else if (positionType === "SHORT") {
                    // For short: equity grows as price falls
                    todayEquity = balance + costBasis + position * (entryPrice - price);

                    // Trailing Stop for Short
                    if (price < peakPrice) {
                        peakPrice = price;
                        stopPrice = peakPrice * (1 + config.trailingStopPct);
                    }

                    let exitPrice = 0;
                    let exitReason = "";
                    let exitTimeSec = today.time;

                    if (low <= takeProfitPrice) {
                        exitPrice = takeProfitPrice;
                        exitReason = "Ambil Untung (Short TP)";
                    } else if (high >= stopPrice) {
                        exitPrice = Math.max(open, stopPrice) * (1 + dynamicSlippage);
                        exitReason = "Batasi Rugi (Short Trailing Stop)";
                    } else if (buySignals[i]) {
                        exitPrice = price * (1 + dynamicSlippage);
                        exitReason = "Sinyal Keluar Tren (Short Cover)";
                    }

                    if (exitPrice > 0) {
                        const grossExit = position * exitPrice;
                        const fee = grossExit * baseFee;
                        const profitUSD = costBasis - grossExit - fee;
                        const profitPct = (profitUSD / costBasis) * 100;

                        trades.push({
                            id: trades.length + 1,
                            entryDate: entryDate,
                            entryTime: entryTime,
                            exitDate: today.date,
                            exitTime: exitTimeSec,
                            type: "SHORT",
                            entryPrice: parseFloat(entryPrice.toFixed(2)),
                            exitPrice: parseFloat(exitPrice.toFixed(2)),
                            pnlUsd: parseFloat(profitUSD.toFixed(2)),
                            pnlPercent: parseFloat(profitPct.toFixed(2)),
                            reason: exitReason
                        });

                        balance += (costBasis + profitUSD);
                        position = 0;
                        positionType = null;
                        costBasis = 0;
                        todayEquity = balance;
                    }
                }
            }

            // 3.2 Entry management (only if no active position)
            if (position === 0 && i >= config.slowPeriod) {
                if (buySignals[i]) {
                    // Entry LONG
                    const budget = balance * config.allocationPct;
                    const slipPrice = price * (1 + dynamicSlippage);
                    const fee = budget * baseFee;
                    const netBudget = budget - fee;

                    position = netBudget / slipPrice;
                    positionType = "LONG";
                    entryPrice = slipPrice;
                    entryDate = today.date;
                    entryTime = today.time;
                    peakPrice = slipPrice;
                    costBasis = netBudget;

                    balance -= budget;
                    todayEquity = balance + (position * price);

                    stopPrice = entryPrice * (1 - config.trailingStopPct);
                    takeProfitPrice = entryPrice * (1 + config.takeProfitPct);
                    executedBuys[i] = true;
                } else if (sellSignals[i]) {
                    // Entry SHORT
                    const budget = balance * config.allocationPct;
                    const slipPrice = price * (1 - dynamicSlippage); // sell order slip decreases execution price
                    const fee = budget * baseFee;
                    const netBudget = budget - fee;

                    position = netBudget / slipPrice;
                    positionType = "SHORT";
                    entryPrice = slipPrice;
                    entryDate = today.date;
                    entryTime = today.time;
                    peakPrice = slipPrice;
                    costBasis = netBudget;

                    balance -= budget;
                    todayEquity = balance + budget; // at entry equity matches margin allocation

                    stopPrice = entryPrice * (1 + config.trailingStopPct);
                    takeProfitPrice = entryPrice * (1 - config.takeProfitPct);
                }
            }

            // Record history
            equityHistory.push({
                time: today.time,
                date: today.date,
                strategy: parseFloat(todayEquity.toFixed(2)),
                buyAndHold: parseFloat((initialCapital / data[0].close * price).toFixed(2))
            });
        }

        // Handle open position at the very end
        let isFinalOpen = false;
        let activeEntryPrice = 0;
        if (position > 0) {
            isFinalOpen = true;
            activeEntryPrice = entryPrice;
            const finalDay = data[dataLength - 1];
            const exitP = finalDay.close;
            
            let profitUSD = 0;
            let profitPct = 0;

            if (positionType === "LONG") {
                const gross = position * exitP;
                const fee = gross * baseFee;
                const net = gross - fee;
                profitUSD = net - costBasis;
                profitPct = (profitUSD / costBasis) * 100;
                balance += net;
            } else {
                const grossExit = position * exitP;
                const fee = grossExit * baseFee;
                profitUSD = costBasis - grossExit - fee;
                profitPct = (profitUSD / costBasis) * 100;
                balance += (costBasis + profitUSD);
            }

            trades.push({
                id: trades.length + 1,
                entryDate: entryDate,
                entryTime: entryTime,
                exitDate: finalDay.date,
                exitTime: finalDay.time,
                type: positionType,
                entryPrice: parseFloat(entryPrice.toFixed(2)),
                exitPrice: parseFloat(exitP.toFixed(2)),
                pnlUsd: parseFloat(profitUSD.toFixed(2)),
                pnlPercent: parseFloat(profitPct.toFixed(2)),
                reason: "Masih Terbuka (Open Trade)"
            });
        }

        // 4. Calculate Advanced Financial Statistics
        let profitFactor = 0;
        let sharpeRatio = 0;
        let riskOfRuin = 0;

        let grossProfit = 0;
        let grossLoss = 0;
        let winningTradesCount = 0;
        let losingTradesCount = 0;
        let totalWinPctSum = 0;
        let totalLossPctSum = 0;

        trades.forEach(t => {
            if (t.reason !== "Masih Terbuka (Open Trade)") {
                if (t.pnlUsd >= 0) {
                    grossProfit += t.pnlUsd;
                    totalWinPctSum += t.pnlPercent;
                    winningTradesCount++;
                } else {
                    grossLoss += Math.abs(t.pnlUsd);
                    totalLossPctSum += Math.abs(t.pnlPercent);
                    losingTradesCount++;
                }
            }
        });

        profitFactor = grossLoss === 0 ? (grossProfit > 0 ? 99.9 : 0) : grossProfit / grossLoss;

        // Sharpe Ratio
        if (equityHistory.length > 2) {
            const dailyPctChanges = [];
            for (let i = 1; i < equityHistory.length; i++) {
                const prev = equityHistory[i - 1].strategy;
                const curr = equityHistory[i].strategy;
                const change = prev === 0 ? 0 : (curr - prev) / prev;
                dailyPctChanges.push(change);
            }

            const meanReturn = dailyPctChanges.reduce((a, b) => a + b, 0) / dailyPctChanges.length;
            const variance = dailyPctChanges.reduce((a, b) => a + Math.pow(b - meanReturn, 2), 0) / (dailyPctChanges.length - 1);
            const stdDev = Math.sqrt(variance);

            // Annualize based on actual timeframe granularity
            const detectedGranularity = data.length > 1 ? (data[1].time - data[0].time) : 86400;
            const periodsPerYear = 31536000 / detectedGranularity;
            sharpeRatio = stdDev === 0 ? 0 : (meanReturn / stdDev) * Math.sqrt(periodsPerYear);
        }

        // Risk of Ruin
        const totalTradesCount = winningTradesCount + losingTradesCount;
        if (totalTradesCount > 0) {
            const winRateDecimal = winningTradesCount / totalTradesCount;
            const avgWinPct = winningTradesCount > 0 ? totalWinPctSum / winningTradesCount : 0;
            const avgLossPct = losingTradesCount > 0 ? totalLossPctSum / losingTradesCount : 1;
            const winToLossRatio = avgLossPct === 0 ? 99.9 : avgWinPct / avgLossPct;

            const edge = (winRateDecimal * winToLossRatio) - (1 - winRateDecimal);
            
            if (edge <= 0) {
                riskOfRuin = 100.0;
            } else {
                riskOfRuin = Math.pow((1 - edge) / (1 + edge), 6) * 100;
                if (riskOfRuin > 100) riskOfRuin = 100;
                if (riskOfRuin < 0.01) riskOfRuin = 0;
            }
        }

        return {
            trades: trades,
            equityHistory: equityHistory,
            finalBalance: balance,
            isFinalOpen: isFinalOpen,
            activeEntryPrice: activeEntryPrice,
            executedBuys: executedBuys,
            config: config,
            fastEma: fastEma,
            slowEma: slowEma,
            rsi: rsi,
            stats: {
                profitFactor: parseFloat(profitFactor.toFixed(2)),
                sharpeRatio: parseFloat(sharpeRatio.toFixed(2)),
                riskOfRuin: parseFloat(riskOfRuin.toFixed(1))
            }
        };
    }

    function runSimulationWithPrecalculated(data, initialCapital, config, precalc) {
        const baseFee = 0.001;
        const baseSlippage = 0.0005;
        const dailyFundingRate = 0.00015;
        const dataLength = data.length;
        
        const fastEma = precalc.ema[config.fastPeriod];
        const slowEma = precalc.ema[config.slowPeriod];
        const rsi = precalc.rsi[config.rsiLength];
        const atr = precalc.atr;

        // Fast signal checks
        const buySignals = new Array(dataLength).fill(false);
        const sellSignals = new Array(dataLength).fill(false);

        for (let i = config.slowPeriod + 1; i < dataLength; i++) {
            const prevFast = fastEma[i - 1];
            const prevSlow = slowEma[i - 1];
            const currFast = fastEma[i];
            const currSlow = slowEma[i];

            if (prevFast <= prevSlow && currFast > currSlow) {
                const currentRsi = rsi[i] || 50;
                if (currentRsi < config.rsiOverbought) {
                    let passFilter = true;
                    if (atr[i]) {
                        const volRatio = atr[i] / data[i].close;
                        if (volRatio < 0.015) passFilter = false;
                    }
                    if (passFilter) buySignals[i] = true;
                }
            } else if (prevFast >= prevSlow && currFast < currSlow) {
                const currentRsi = rsi[i] || 50;
                if (currentRsi > config.rsiOversold) {
                    let passFilter = true;
                    if (atr[i]) {
                        const volRatio = atr[i] / data[i].close;
                        if (volRatio < 0.015) passFilter = false;
                    }
                    if (passFilter) sellSignals[i] = true;
                }
            }
        }

        let balance = initialCapital;
        let position = 0;
        let positionType = null;
        let entryPrice = 0;
        let peakPrice = 0;
        let stopPrice = 0;
        let takeProfitPrice = 0;
        let costBasis = 0;

        for (let i = 0; i < dataLength; i++) {
            const price = data[i].close;
            const high = data[i].high;
            const low = data[i].low;
            const open = data[i].open;

            // Slippage
            const currentAtr = atr[i] || (price * 0.02);
            const dynamicSlippage = baseSlippage * (1 + (currentAtr / price) * 40) * 1.004; // standard orderSize 1000

            if (position > 0) {
                const fundingFee = (position * price) * dailyFundingRate;
                balance -= fundingFee;

                if (positionType === "LONG") {
                    if (price > peakPrice) {
                        peakPrice = price;
                        stopPrice = peakPrice * (1 - config.trailingStopPct);
                    }

                    let exitPrice = 0;
                    if (high >= takeProfitPrice) {
                        exitPrice = takeProfitPrice;
                    } else if (low <= stopPrice) {
                        exitPrice = Math.min(open, stopPrice) * (1 - dynamicSlippage);
                    } else if (sellSignals[i]) {
                        exitPrice = price * (1 - dynamicSlippage);
                    }

                    if (exitPrice > 0) {
                        const gross = position * exitPrice;
                        balance += gross - (gross * baseFee);
                        position = 0;
                        positionType = null;
                    }
                } else if (positionType === "SHORT") {
                    if (price < peakPrice) {
                        peakPrice = price;
                        stopPrice = peakPrice * (1 + config.trailingStopPct);
                    }

                    let exitPrice = 0;
                    if (low <= takeProfitPrice) {
                        exitPrice = takeProfitPrice;
                    } else if (high >= stopPrice) {
                        exitPrice = Math.max(open, stopPrice) * (1 + dynamicSlippage);
                    } else if (buySignals[i]) {
                        exitPrice = price * (1 + dynamicSlippage);
                    }

                    if (exitPrice > 0) {
                        const grossExit = position * exitPrice;
                        const profitUSD = costBasis - grossExit - (grossExit * baseFee);
                        balance += costBasis + profitUSD;
                        position = 0;
                        positionType = null;
                    }
                }
            }

            if (position === 0 && i >= config.slowPeriod) {
                if (buySignals[i]) {
                    const budget = balance * config.allocationPct;
                    const slipPrice = price * (1 + dynamicSlippage);
                    position = (budget - (budget * baseFee)) / slipPrice;
                    positionType = "LONG";
                    entryPrice = slipPrice;
                    peakPrice = slipPrice;
                    costBasis = budget - (budget * baseFee);
                    balance -= budget;
                    stopPrice = entryPrice * (1 - config.trailingStopPct);
                    takeProfitPrice = entryPrice * (1 + config.takeProfitPct);
                } else if (sellSignals[i]) {
                    const budget = balance * config.allocationPct;
                    const slipPrice = price * (1 - dynamicSlippage);
                    position = (budget - (budget * baseFee)) / slipPrice;
                    positionType = "SHORT";
                    entryPrice = slipPrice;
                    peakPrice = slipPrice;
                    costBasis = budget - (budget * baseFee);
                    balance -= budget;
                    stopPrice = entryPrice * (1 + config.trailingStopPct);
                    takeProfitPrice = entryPrice * (1 - config.takeProfitPct);
                }
            }
        }

        // Final open position liquid
        if (position > 0) {
            const exitP = data[dataLength - 1].close;
            if (positionType === "LONG") {
                const gross = position * exitP;
                balance += gross - (gross * baseFee);
            } else {
                const grossExit = position * exitP;
                const profitUSD = costBasis - grossExit - (grossExit * baseFee);
                balance += costBasis + profitUSD;
            }
        }

        return balance;
    }

    function optimizeParameters(data, initialCapital) {
        const dataLength = data.length;
        if (dataLength < 50) return null;

        // Search options
        const emaFastChoices = [5, 8, 12, 15];
        const emaSlowChoices = [21, 26, 35, 50];
        const rsiLenChoices = [10, 14, 20];
        const rsiOsChoices = [30, 35, 40];
        const rsiObChoices = [65, 70, 75];
        const tpChoices = [5, 8, 12, 15];
        const tsChoices = [1.5, 2.5, 3.5, 5.0];
        const allocChoices = [50, 65, 80, 100];

        // 1. Precalculate indicators
        const closes = data.map(d => d.close);
        const precalc = {
            ema: {},
            rsi: {},
            atr: window.IndicatorEngine.atr(data, 14)
        };

        emaFastChoices.forEach(p => {
            precalc.ema[p] = window.IndicatorEngine.ema(closes, p);
        });
        emaSlowChoices.forEach(p => {
            precalc.ema[p] = window.IndicatorEngine.ema(closes, p);
        });
        rsiLenChoices.forEach(p => {
            precalc.rsi[p] = window.IndicatorEngine.rsi(closes, p);
        });

        // 2. Perform Grid Search
        let bestBalance = -1;
        let bestConfig = null;

        for (let fast of emaFastChoices) {
            for (let slow of emaSlowChoices) {
                if (slow <= fast) continue;
                for (let rsiL of rsiLenChoices) {
                    for (let rsiOs of rsiOsChoices) {
                        for (let rsiOb of rsiObChoices) {
                            for (let tp of tpChoices) {
                                for (let ts of tsChoices) {
                                    for (let alloc of allocChoices) {
                                        const config = {
                                            fastPeriod: fast,
                                            slowPeriod: slow,
                                            rsiLength: rsiL,
                                            rsiOversold: rsiOs,
                                            rsiOverbought: rsiOb,
                                            takeProfitPct: tp / 100,
                                            trailingStopPct: ts / 100,
                                            allocationPct: alloc / 100
                                        };

                                        const finalBalance = runSimulationWithPrecalculated(data, initialCapital, config, precalc);
                                        
                                        if (finalBalance > bestBalance) {
                                            bestBalance = finalBalance;
                                            bestConfig = {
                                                emaFast: fast,
                                                emaSlow: slow,
                                                rsiLen: rsiL,
                                                rsiOs: rsiOs,
                                                rsiOb: rsiOb,
                                                tp: tp,
                                                ts: ts,
                                                alloc: alloc,
                                                roi: ((finalBalance - initialCapital) / initialCapital) * 100
                                            };
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }

        return bestConfig;
    }

    // Expose to window
    window.BacktestEngine = {
        run: runBacktestSimulation,
        optimize: optimizeParameters
    };
})();
