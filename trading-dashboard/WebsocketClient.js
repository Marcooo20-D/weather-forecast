/**
 * AURA Quant - Multi-Source WebSocket & HTTP Polling Live Client
 * Connects to Coinbase Pro WS -> falls back to Bybit Spot WS -> falls back to HTTP Polling
 * Exposes connections as window.WebsocketClient
 */
(function() {
    let activeSocket = null;
    let fallbackTimeout = null;
    let pollInterval = null;
    let isConnected = false;

    function connectLiveStream(productId, onTick, onConnect, onDisconnect) {
        disconnectLiveStream();
        isConnected = false;

        // Try Coinbase WS first
        console.log(`Connecting to Coinbase WebSocket for ${productId}...`);
        const wsUrl = "wss://ws-feed.exchange.coinbase.com";
        const socket = new WebSocket(wsUrl);
        activeSocket = socket;

        // Fallback timer: if not connected/receiving data in 4 seconds, switch to Bybit WS
        fallbackTimeout = setTimeout(() => {
            if (!isConnected && activeSocket === socket) {
                console.warn("Coinbase WebSocket connection timed out. Falling back to Bybit Spot WebSocket...");
                socket.close();
                connectBybit(productId, onTick, onConnect, onDisconnect);
            }
        }, 4000);

        socket.onopen = function() {
            const subscribePayload = {
                type: "subscribe",
                product_ids: [productId],
                channels: ["ticker"]
            };
            socket.send(JSON.stringify(subscribePayload));
        };

        socket.onmessage = function(event) {
            try {
                const message = JSON.parse(event.data);
                if (message.type !== "ticker") return;

                if (!isConnected) {
                    isConnected = true;
                    clearTimeout(fallbackTimeout);
                    console.log("Coinbase WebSocket successfully receiving ticks.");
                    if (onConnect) onConnect();
                }

                const date = new Date(message.time);
                const timeSec = Math.floor(date.getTime() / 1000);
                const dateStr = date.toISOString().substring(0, 16).replace('T', ' ');

                const tick = {
                    time: timeSec,
                    date: dateStr,
                    price: parseFloat(message.price),
                    open: parseFloat(message.open_24h || message.price),
                    high: parseFloat(message.high_24h || message.price),
                    low: parseFloat(message.low_24h || message.price),
                    volume: parseFloat(message.volume_24h || 100)
                };

                if (onTick) onTick(tick);
            } catch (err) {
                console.error("Error parsing Coinbase WS tick:", err);
            }
        };

        socket.onerror = function(err) {
            console.warn("Coinbase WebSocket error, triggering Bybit WS fallback...", err);
            if (!isConnected && activeSocket === socket) {
                clearTimeout(fallbackTimeout);
                connectBybit(productId, onTick, onConnect, onDisconnect);
            }
        };

        socket.onclose = function() {
            console.log("Coinbase WebSocket connection closed.");
            if (!isConnected && activeSocket === socket) {
                clearTimeout(fallbackTimeout);
                connectBybit(productId, onTick, onConnect, onDisconnect);
            } else if (isConnected && activeSocket === socket) {
                if (onDisconnect) onDisconnect();
            }
        };
    }

    function connectBybit(productId, onTick, onConnect, onDisconnect) {
        disconnectLiveStream();
        isConnected = false;

        const bybitSymbols = {
            "BTC-USD": "BTCUSDT",
            "ETH-USD": "ETHUSDT",
            "SOL-USD": "SOLUSDT",
            "BNB-USD": "BNBUSDT"
        };
        const symbol = bybitSymbols[productId] || "BTCUSDT";

        console.log(`Connecting to Bybit WebSocket for ${symbol}...`);
        const wsUrl = "wss://stream.bybit.com/v5/public/spot";
        const socket = new WebSocket(wsUrl);
        activeSocket = socket;

        // Fallback timer: if Bybit WS doesn't connect/receive data in 4 seconds, switch to HTTP Polling
        fallbackTimeout = setTimeout(() => {
            if (!isConnected && activeSocket === socket) {
                console.warn("Bybit WebSocket timed out. Falling back to HTTP Polling...");
                socket.close();
                startPolling(productId, onTick, onConnect, onDisconnect);
            }
        }, 4000);

        socket.onopen = function() {
            const subscribePayload = {
                op: "subscribe",
                args: [`ticker.${symbol}`]
            };
            socket.send(JSON.stringify(subscribePayload));
        };

        socket.onmessage = function(event) {
            try {
                const message = JSON.parse(event.data);
                if (message.topic !== `ticker.${symbol}` || !message.data) return;

                if (!isConnected) {
                    isConnected = true;
                    clearTimeout(fallbackTimeout);
                    console.log("Bybit WebSocket successfully receiving ticks.");
                    if (onConnect) onConnect();
                }

                const data = message.data;
                const timeSec = Math.floor(message.ts / 1000);
                const date = new Date(message.ts);
                const dateStr = date.toISOString().substring(0, 16).replace('T', ' ');

                const tick = {
                    time: timeSec,
                    date: dateStr,
                    price: parseFloat(data.lastPrice),
                    open: parseFloat(data.prevPrice24h || data.lastPrice),
                    high: parseFloat(data.highPrice24h || data.lastPrice),
                    low: parseFloat(data.lowPrice24h || data.lastPrice),
                    volume: parseFloat(data.volume24h || 100)
                };

                if (onTick) onTick(tick);
            } catch (err) {
                console.error("Error parsing Bybit WS tick:", err);
            }
        };

        socket.onerror = function(err) {
            console.warn("Bybit WebSocket error, triggering HTTP polling...", err);
            if (!isConnected && activeSocket === socket) {
                clearTimeout(fallbackTimeout);
                startPolling(productId, onTick, onConnect, onDisconnect);
            }
        };

        socket.onclose = function() {
            console.log("Bybit WebSocket connection closed.");
            if (!isConnected && activeSocket === socket) {
                clearTimeout(fallbackTimeout);
                startPolling(productId, onTick, onConnect, onDisconnect);
            } else if (isConnected && activeSocket === socket) {
                if (onDisconnect) onDisconnect();
            }
        };
    }

    function startPolling(productId, onTick, onConnect, onDisconnect) {
        disconnectLiveStream();
        console.log(`Starting HTTP Polling for ${productId}...`);

        const coinGeckoIds = {
            "BTC-USD": "bitcoin",
            "ETH-USD": "ethereum",
            "SOL-USD": "solana",
            "BNB-USD": "binancecoin"
        };
        const coinId = coinGeckoIds[productId] || "bitcoin";
        let lastPrice = null;

        // Immediately trigger first fetch
        pollFetch();
        if (onConnect) onConnect();

        // Poll every 4 seconds
        pollInterval = setInterval(pollFetch, 4000);

        async function pollFetch() {
            try {
                // Try CoinGecko first as it is unblocked and has CORS
                const url = `https://api.coingecko.com/api/v3/simple/price?ids=${coinId}&vs_currencies=usd`;
                const response = await fetch(url);
                if (!response.ok) throw new Error(`CoinGecko HTTP ${response.status}`);
                
                const data = await response.json();
                const price = parseFloat(data[coinId].usd);
                
                if (isNaN(price)) throw new Error("Invalid price returned from CoinGecko");

                const nowMs = Date.now();
                const tick = {
                    time: Math.floor(nowMs / 1000),
                    date: new Date(nowMs).toISOString().substring(0, 16).replace('T', ' '),
                    price: price,
                    open: lastPrice || price,
                    high: price,
                    low: price,
                    volume: 1000
                };

                lastPrice = price;
                if (onTick) onTick(tick);
            } catch (err) {
                console.warn("CoinGecko polling failed, trying Bybit API...", err);
                try {
                    const bybitSymbols = {
                        "BTC-USD": "BTCUSDT",
                        "ETH-USD": "ETHUSDT",
                        "SOL-USD": "SOLUSDT",
                        "BNB-USD": "BNBUSDT"
                    };
                    const symbol = bybitSymbols[productId] || "BTCUSDT";
                    const url = `https://api.bybit.com/v5/market/tickers?category=spot&symbol=${symbol}`;
                    const response = await fetch(url);
                    if (!response.ok) throw new Error(`Bybit HTTP ${response.status}`);
                    
                    const json = await response.json();
                    if (json.retCode !== 0 || !json.result || !json.result.list || json.result.list.length === 0) {
                        throw new Error(json.retMsg || "Empty Bybit results");
                    }
                    
                    const ticker = json.result.list[0];
                    const price = parseFloat(ticker.lastPrice);

                    const nowMs = Date.now();
                    const tick = {
                        time: Math.floor(nowMs / 1000),
                        date: new Date(nowMs).toISOString().substring(0, 16).replace('T', ' '),
                        price: price,
                        open: parseFloat(ticker.prevPrice24h || ticker.lastPrice),
                        high: parseFloat(ticker.highPrice24h || ticker.lastPrice),
                        low: parseFloat(ticker.lowPrice24h || ticker.lastPrice),
                        volume: parseFloat(ticker.volume24h || 100)
                    };

                    if (onTick) onTick(tick);
                } catch (e2) {
                    console.error("All polling fallbacks failed:", e2);
                }
            }
        }
    }

    function disconnectLiveStream() {
        clearTimeout(fallbackTimeout);
        if (pollInterval) {
            clearInterval(pollInterval);
            pollInterval = null;
        }
        if (activeSocket) {
            activeSocket.onclose = null;
            activeSocket.onerror = null;
            activeSocket.close();
            activeSocket = null;
        }
    }

    window.WebsocketClient = {
        connect: connectLiveStream,
        disconnect: disconnectLiveStream
    };
})();
