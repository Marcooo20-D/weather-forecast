/**
 * AURA Quant - User Interface & General UI Manager Module
 * Exposes core UI functions as window.AuraUI
 */
(function() {
    // Toast notifications queue
    const toastContainer = document.getElementById("toast-container");

    /**
     * Shows a cyberpunk neon toast pop-up
     */
    function showToast(title, desc, type = "info") {
        const toast = document.createElement("div");
        toast.className = `toast ${type}`;
        
        let iconSvg = '';
        if (type === "success") {
            iconSvg = `<svg class="toast-icon success" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"></polyline></svg>`;
        } else if (type === "danger") {
            iconSvg = `<svg class="toast-icon danger" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="8" x2="12" y2="12"></line><line x1="12" y1="16" x2="12.01" y2="16"></line></svg>`;
        } else {
            iconSvg = `<svg class="toast-icon info" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="16" x2="12" y2="12"></line><line x1="12" y1="8" x2="12.01" y2="8"></svg>`;
        }

        toast.innerHTML = `
            ${iconSvg}
            <div class="toast-body">
                <div class="toast-title">${title}</div>
                <div class="toast-desc">${desc}</div>
            </div>
        `;

        toastContainer.appendChild(toast);
        setTimeout(() => toast.classList.add("show"), 50);

        setTimeout(() => {
            toast.classList.remove("show");
            setTimeout(() => toast.remove(), 400);
        }, 6000);
    }

    /**
     * Generates realistic mock data for offline fallback
     */
    function generateMockData(productId, days = 600, granularity = 86400) {
        let startPrice = 60000;
        let volatility = 0.03;
        let trend = 0.005;
        let seed = 42;

        if (productId.includes("ETH")) {
            startPrice = 3000;
            volatility = 0.035;
            trend = 0.003;
            seed = 137;
        } else if (productId.includes("SOL")) {
            startPrice = 140;
            volatility = 0.055;
            trend = 0.008;
            seed = 789;
        }

        let currentSeed = seed;
        function rand() {
            currentSeed = (currentSeed * 1664525 + 1013904223) % 4294967296;
            return currentSeed / 4294967296;
        }

        const data = [];
        let currentPrice = startPrice;
        
        const stepMs = granularity * 1000;
        const nowMs = Date.now();
        const startDate = new Date(nowMs - days * stepMs);

        for (let i = 0; i < days; i++) {
            const date = new Date(startDate.getTime() + i * stepMs);
            const timeSec = Math.floor(date.getTime() / 1000);
            
            const dateStr = (granularity < 86400) 
                ? date.toISOString().substring(0, 16).replace('T', ' ')
                : date.toISOString().split('T')[0];

            const open = currentPrice;
            const changePercent = (rand() - 0.48 + trend) * volatility;
            let close = open * (1 + changePercent);
            
            if (close < startPrice * 0.05) close = startPrice * 0.05;

            const maxDev = open * volatility * 0.8;
            const high = Math.max(open, close) + rand() * maxDev;
            const low = Math.max(open * 0.01, Math.min(open, close) - rand() * maxDev);
            const volume = Math.round((productId.includes("BTC") ? 15000 : productId.includes("ETH") ? 120000 : 800000) * (0.5 + rand() * 1.5));

            data.push({
                time: timeSec,
                date: dateStr,
                open: parseFloat(open.toFixed(2)),
                high: parseFloat(high.toFixed(2)),
                low: parseFloat(low.toFixed(2)),
                close: parseFloat(close.toFixed(2)),
                volume: volume
            });

            currentPrice = close;
        }
        return data;
    }

    /**
     * Fetches real historical daily candles from Coinbase REST API (Chunked & Cached)
     */
    function saveCache(key, timeKey, data) {
        try {
            localStorage.setItem(key, JSON.stringify(data));
            localStorage.setItem(timeKey, Date.now().toString());
        } catch (e) {
            console.warn("Could not cache candles to localStorage:", e);
        }
    }

    function getBackendBaseUrl() {
        const backend = window.AuraConfig && window.AuraConfig.backend;
        if (!backend || backend.enabled === false) return null;
        return (backend.baseUrl || "").replace(/\/$/, "");
    }

    function productToExchangeSymbol(productId) {
        const mappings = window.AuraConfig && window.AuraConfig.exchangeSymbols;
        if (mappings && mappings[productId]) return mappings[productId];
        const normalized = String(productId || "BTC-USD").replace("-USD", "USDT").toUpperCase();
        return normalized.endsWith("USDT") ? normalized : `${normalized}USDT`;
    }

    async function fetchFromBackend(productId, granularity) {
        const baseUrl = getBackendBaseUrl();
        if (baseUrl === null) throw new Error("Backend bridge disabled.");

        const symbol = productToExchangeSymbol(productId);
        const url = `${baseUrl}/api/candles?symbol=${encodeURIComponent(symbol)}&granularity=${encodeURIComponent(granularity)}&limit=1000`;
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 5000);

        const response = await fetch(url, { signal: controller.signal });
        clearTimeout(timeoutId);

        if (!response.ok) throw new Error(`Backend HTTP ${response.status}`);

        const payload = await response.json();
        if (!payload.candles || !Array.isArray(payload.candles) || payload.candles.length === 0) {
            throw new Error("Backend returned no candles.");
        }
        return payload.candles;
    }

    async function fetchModelSignal(assetOrProductId) {
        const backend = window.AuraConfig && window.AuraConfig.backend;
        const baseUrl = getBackendBaseUrl();
        if (baseUrl === null || !backend.signalEnabled) return null;

        const symbol = productToExchangeSymbol(assetOrProductId);
        const url = `${baseUrl}/api/signal?symbol=${encodeURIComponent(symbol)}`;
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 5000);

        try {
            const response = await fetch(url, { signal: controller.signal });
            clearTimeout(timeoutId);
            if (!response.ok) return null;
            return await response.json();
        } catch (err) {
            clearTimeout(timeoutId);
            console.warn("Backend signal unavailable.", err);
            return null;
        }
    }

    async function fetchFromCoinbase(productId, granularity) {
        const candlesPerChunk = 300;
        const chunkDurationMs = candlesPerChunk * granularity * 1000;
        const allData = [];
        const now = Date.now();
        
        for (let chunk = 0; chunk < 2; chunk++) {
            const end = new Date(now - chunk * chunkDurationMs).toISOString();
            const start = new Date(now - (chunk + 1) * chunkDurationMs).toISOString();
            
            const url = `https://api.exchange.coinbase.com/products/${productId}/candles?granularity=${granularity}&start=${start}&end=${end}`;
            
            const controller = new AbortController();
            const timeoutId = setTimeout(() => controller.abort(), 4000);
            
            const response = await fetch(url, { signal: controller.signal });
            clearTimeout(timeoutId);
            
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}: ${response.statusText}`);
            }
            
            const data = await response.json();
            if (Array.isArray(data)) {
                allData.push(...data);
            }
            
            await new Promise(r => setTimeout(r, 200));
        }
        
        if (allData.length === 0) {
            throw new Error("No data returned from Coinbase.");
        }

        const uniqueBars = new Map();
        allData.forEach(item => {
            const timestamp = item[0];
            if (!uniqueBars.has(timestamp)) {
                uniqueBars.set(timestamp, item);
            }
        });

        const sorted = Array.from(uniqueBars.values()).sort((a, b) => a[0] - b[0]);
        
        return sorted.map(item => {
            const date = new Date(item[0] * 1000);
            const dateStr = (granularity < 86400) 
                ? date.toISOString().substring(0, 16).replace('T', ' ')
                : date.toISOString().split('T')[0];

            return {
                time: item[0],
                date: dateStr,
                low: item[1],
                high: item[2],
                open: item[3],
                close: item[4],
                volume: item[5]
            };
        });
    }

    async function fetchFromKraken(productId, granularity) {
        const krakenPairs = {
            "BTC-USD": "XXBTZUSD",
            "ETH-USD": "XETHZUSD",
            "SOL-USD": "SOLUSD",
            "BNB-USD": "BNBUSD"
        };
        const pair = krakenPairs[productId] || "XXBTZUSD";
        const interval = Math.floor(granularity / 60);
        
        const url = `https://api.kraken.com/0/public/OHLC?pair=${pair}&interval=${interval}`;
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 4000);
        
        const response = await fetch(url, { signal: controller.signal });
        clearTimeout(timeoutId);
        
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}: ${response.statusText}`);
        }
        
        const json = await response.json();
        if (json.error && json.error.length > 0) {
            throw new Error(json.error.join(", "));
        }
        
        const keys = Object.keys(json.result);
        const dataKey = keys.find(k => k !== "last");
        const rawCandles = json.result[dataKey];
        
        if (!rawCandles || rawCandles.length === 0) {
            throw new Error("No data returned from Kraken.");
        }
        
        return rawCandles.map(c => {
            const timeSec = parseInt(c[0]);
            const date = new Date(timeSec * 1000);
            const dateStr = (granularity < 86400) 
                ? date.toISOString().substring(0, 16).replace('T', ' ')
                : date.toISOString().split('T')[0];
            return {
                time: timeSec,
                date: dateStr,
                open: parseFloat(c[1]),
                high: parseFloat(c[2]),
                low: parseFloat(c[3]),
                close: parseFloat(c[4]),
                volume: parseFloat(c[6])
            };
        });
    }

    async function fetchFromBybit(productId, granularity) {
        const bybitSymbols = {
            "BTC-USD": "BTCUSDT",
            "ETH-USD": "ETHUSDT",
            "SOL-USD": "SOLUSDT",
            "BNB-USD": "BNBUSDT"
        };
        const symbol = bybitSymbols[productId] || "BTCUSDT";
        
        let interval = "D";
        if (granularity === 21600) interval = "360";
        else if (granularity === 3600) interval = "60";
        else interval = Math.floor(granularity / 60).toString();
        
        const url = `https://api.bybit.com/v5/market/kline?category=spot&symbol=${symbol}&interval=${interval}&limit=1000`;
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 4000);
        
        const response = await fetch(url, { signal: controller.signal });
        clearTimeout(timeoutId);
        
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}: ${response.statusText}`);
        }
        
        const json = await response.json();
        if (json.retCode !== 0) {
            throw new Error(json.retMsg);
        }
        
        const rawCandles = json.result.list;
        if (!rawCandles || rawCandles.length === 0) {
            throw new Error("No data returned from Bybit.");
        }
        
        return rawCandles.reverse().map(c => {
            const timeSec = Math.floor(parseInt(c[0]) / 1000);
            const date = new Date(timeSec * 1000);
            const dateStr = (granularity < 86400) 
                ? date.toISOString().substring(0, 16).replace('T', ' ')
                : date.toISOString().split('T')[0];
            return {
                time: timeSec,
                date: dateStr,
                open: parseFloat(c[1]),
                high: parseFloat(c[2]),
                low: parseFloat(c[3]),
                close: parseFloat(c[4]),
                volume: parseFloat(c[5])
            };
        });
    }

    async function fetchFromCoinGecko(productId, granularity) {
        const coinGeckoIds = {
            "BTC-USD": "bitcoin",
            "ETH-USD": "ethereum",
            "SOL-USD": "solana",
            "BNB-USD": "binancecoin"
        };
        const coinId = coinGeckoIds[productId] || "bitcoin";
        const url = `https://api.coingecko.com/api/v3/coins/${coinId}/ohlc?vs_currency=usd&days=365`;
        
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 4000);
        
        const response = await fetch(url, { signal: controller.signal });
        clearTimeout(timeoutId);
        
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}: ${response.statusText}`);
        }
        
        const rawCandles = await response.json();
        if (!Array.isArray(rawCandles) || rawCandles.length === 0) {
            throw new Error("No data returned from CoinGecko.");
        }
        
        return rawCandles.map(c => {
            const timeSec = Math.floor(c[0] / 1000);
            const date = new Date(timeSec * 1000);
            const dateStr = (granularity < 86400) 
                ? date.toISOString().substring(0, 16).replace('T', ' ')
                : date.toISOString().split('T')[0];
            return {
                time: timeSec,
                date: dateStr,
                open: c[1],
                high: c[2],
                low: c[3],
                close: c[4],
                volume: 100000 // default dummy volume since CoinGecko OHLC doesn't provide it
            };
        });
    }

    /**
     * Fetches real historical daily candles from multiple API providers sequentially
     */
    async function fetchHistoricalData(productId, granularity = 86400) {
        const cacheKey = `aura_cache_${productId}_${granularity}`;
        const cacheTimeKey = `${cacheKey}_time`;
        
        const cachedData = localStorage.getItem(cacheKey);
        const cachedTime = localStorage.getItem(cacheTimeKey);
        const now = Date.now();

        try {
            console.log(`Trying local research backend for ${productId}...`);
            const data = await fetchFromBackend(productId, granularity);
            saveCache(cacheKey, cacheTimeKey, data);
            showToast("Research Backend Aktif", "Data historis diambil dari cache riset lokal.", "success");
            return data;
        } catch (err) {
            console.warn("Local research backend unavailable. Falling back to public exchange APIs...", err);
        }
        
        // Cache validity: 2 hours
        if (cachedData && cachedTime && (now - parseInt(cachedTime) < 2 * 3600 * 1000)) {
            console.log(`Using cached data for ${productId} (${granularity})`);
            try {
                return JSON.parse(cachedData);
            } catch (e) {
                console.error("Failed to parse cached candles:", e);
            }
        }

        // Try Coinbase API
        try {
            console.log(`Trying Coinbase REST API for ${productId}...`);
            const data = await fetchFromCoinbase(productId, granularity);
            saveCache(cacheKey, cacheTimeKey, data);
            return data;
        } catch (err) {
            console.warn("Coinbase API failed or timed out. Falling back to Bybit API...", err);
        }

        // Try Bybit API (CORS friendly and real-time Spot)
        try {
            console.log(`Trying Bybit API for ${productId}...`);
            const data = await fetchFromBybit(productId, granularity);
            saveCache(cacheKey, cacheTimeKey, data);
            showToast("API Fallback Aktif", "Mengambil data pasar riil dari server Bybit.", "info");
            return data;
        } catch (err) {
            console.warn("Bybit API failed. Falling back to Kraken API...", err);
        }

        // Try Kraken API (CORS friendly and real-time Spot)
        try {
            console.log(`Trying Kraken API for ${productId}...`);
            const data = await fetchFromKraken(productId, granularity);
            saveCache(cacheKey, cacheTimeKey, data);
            showToast("API Fallback Aktif", "Mengambil data pasar riil dari server Kraken.", "info");
            return data;
        } catch (err) {
            console.warn("Kraken API failed. Falling back to CoinGecko API...", err);
        }

        // Try CoinGecko API (CORS friendly but cached/delayed OHLC)
        try {
            console.log(`Trying CoinGecko API for ${productId}...`);
            const data = await fetchFromCoinGecko(productId, granularity);
            saveCache(cacheKey, cacheTimeKey, data);
            showToast("API Fallback Aktif", "Mengambil data pasar riil dari CoinGecko API.", "info");
            return data;
        } catch (err) {
            console.warn("CoinGecko API failed. Beralih ke data simulasi lokal.", err);
        }

        // All API calls failed, fall back to simulated data
        showToast("Mode Offline Aktif", "Gagal mengambil data dari server bursa. Menggunakan data simulasi lokal.", "warning");
        return generateMockData(productId, 600, granularity);
    }

    /**
     * Renders monthly return matrices heatmap
     */
    function renderHeatmap(equityHistory, startCapital) {
        const heatmapBody = document.getElementById("heatmap-body");
        heatmapBody.innerHTML = "";
        
        const monthlyData = {};
        equityHistory.forEach(eq => {
            const date = new Date(eq.date);
            const year = date.getFullYear();
            const month = date.getMonth();

            if (!monthlyData[year]) monthlyData[year] = {};
            monthlyData[year][month] = eq.strategy;
        });

        const years = Object.keys(monthlyData).sort((a, b) => b - a);

        years.forEach(year => {
            const tr = document.createElement("tr");
            
            const tdYear = document.createElement("td");
            tdYear.innerHTML = `<strong>${year}</strong>`;
            tr.appendChild(tdYear);

            let yearStartCapital = 0;
            let yearEndCapital = 0;
            let firstMonthOfData = null;

            for (let month = 0; month < 12; month++) {
                const td = document.createElement("td");
                
                if (monthlyData[year][month] !== undefined) {
                    let prevEquity = 0;
                    
                    if (month === 0) {
                        const prevYear = parseInt(year) - 1;
                        if (monthlyData[prevYear] && monthlyData[prevYear][11] !== undefined) {
                            prevEquity = monthlyData[prevYear][11];
                        } else {
                            prevEquity = startCapital;
                        }
                    } else {
                        let checkMonth = month - 1;
                        while (checkMonth >= 0 && monthlyData[year][checkMonth] === undefined) checkMonth--;
                        
                        if (checkMonth >= 0) {
                            prevEquity = monthlyData[year][checkMonth];
                        } else {
                            const prevYear = parseInt(year) - 1;
                            if (monthlyData[prevYear] && monthlyData[prevYear][11] !== undefined) {
                                prevEquity = monthlyData[prevYear][11];
                            } else {
                                prevEquity = startCapital;
                            }
                        }
                    }

                    const currentEquity = monthlyData[year][month];
                    const diffPct = ((currentEquity - prevEquity) / prevEquity) * 100;
                    
                    if (firstMonthOfData === null) firstMonthOfData = prevEquity;
                    yearEndCapital = currentEquity;

                    td.textContent = `${diffPct >= 0 ? '+' : ''}${diffPct.toFixed(1)}%`;
                    
                    if (diffPct > 5) td.className = "hm-positive-x";
                    else if (diffPct > 0) td.className = "hm-positive";
                    else if (diffPct < -5) td.className = "hm-negative-x";
                    else if (diffPct < 0) td.className = "hm-negative";
                    else td.className = "hm-neutral";
                } else {
                    td.textContent = "-";
                    td.className = "hm-neutral";
                }
                tr.appendChild(td);
            }

            const tdYtd = document.createElement("td");
            tdYtd.className = "ytd-col";
            
            if (firstMonthOfData > 0) {
                const ytdPct = ((yearEndCapital - firstMonthOfData) / firstMonthOfData) * 100;
                tdYtd.textContent = `${ytdPct >= 0 ? '+' : ''}${ytdPct.toFixed(1)}%`;
                tdYtd.className = `ytd-col ${ytdPct >= 0 ? 'positive' : 'negative'}`;
            } else {
                tdYtd.textContent = "-";
            }
            tr.appendChild(tdYtd);
            heatmapBody.appendChild(tr);
        });
    }

    /**
     * Saves simulation parameters state to localStorage
     */
    function saveState(capital, asset, profile, timeframe, orderSize, whipsawFilter) {
        localStorage.setItem("aura_capital", capital);
        localStorage.setItem("aura_asset", asset);
        localStorage.setItem("aura_profile", profile);
        localStorage.setItem("aura_timeframe", timeframe);
        localStorage.setItem("aura_order_size", orderSize);
        localStorage.setItem("aura_whipsaw_filter", whipsawFilter);
    }

    /**
     * Restores state from localStorage
     */
    function loadState() {
        return {
            capital: localStorage.getItem("aura_capital") || "10000",
            asset: localStorage.getItem("aura_asset") || "BTC",
            profile: localStorage.getItem("aura_profile") || "moderate",
            timeframe: localStorage.getItem("aura_timeframe") || "86400",
            orderSize: localStorage.getItem("aura_order_size") || "1000",
            whipsawFilter: localStorage.getItem("aura_whipsaw_filter") !== "false"
        };
    }

    // Expose elements
    window.AuraUI = {
        showToast: showToast,
        fetchData: fetchHistoricalData,
        fetchSignal: fetchModelSignal,
        renderHeatmap: renderHeatmap,
        saveState: saveState,
        loadState: loadState
    };
})();
