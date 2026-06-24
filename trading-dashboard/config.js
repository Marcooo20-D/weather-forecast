/**
 * AURA Quant - System Settings & Profiles Configuration
 */
(function() {
    window.AuraConfig = {
        // Defined risk style settings
        profiles: {
            conserv: {
                name: "Gaya Konservatif",
                fastPeriod: 20,
                slowPeriod: 50,
                rsiLength: 14,
                rsiOversold: 35,
                rsiOverbought: 65,
                takeProfitPct: 0.12,  // 12%
                trailingStopPct: 0.04, // 4%
                allocationPct: 0.35,   // Use 35% of capital per trade
                description: "Menghindari risiko tinggi, fokus pada tren yang sudah terkonfirmasi kuat."
            },
            moderate: {
                name: "Gaya Moderat",
                fastPeriod: 12,
                slowPeriod: 26,
                rsiLength: 14,
                rsiOversold: 40,
                rsiOverbought: 70,
                takeProfitPct: 0.08,   // 8%
                trailingStopPct: 0.025, // 2.5%
                allocationPct: 0.65,    // Use 65% of capital
                description: "Keseimbangan optimal antara keamanan modal dan imbal hasil."
            },
            aggr: {
                name: "Gaya Agresif",
                fastPeriod: 7,
                slowPeriod: 15,
                rsiLength: 14,
                rsiOversold: 45,
                rsiOverbought: 75,
                takeProfitPct: 0.05,   // 5%
                trailingStopPct: 0.015, // 1.5%
                allocationPct: 1.00,    // Use 100% of capital (All-in)
                description: "Sangat responsif pada pergerakan jangka pendek demi mengejar profit maksimal."
            }
        },
        
        // Product mappings between symbol selected and Coinbase Product IDs
        products: {
            BTC: "BTC-USD",
            ETH: "ETH-USD",
            SOL: "SOL-USD",
            BNB: "BNB-USD"
        },

        exchangeSymbols: {
            BTC: "BTCUSDT",
            ETH: "ETHUSDT",
            SOL: "SOLUSDT",
            BNB: "BNBUSDT",
            "BTC-USD": "BTCUSDT",
            "ETH-USD": "ETHUSDT",
            "SOL-USD": "SOLUSDT",
            "BNB-USD": "BNBUSDT"
        },

        backend: {
            enabled: true,
            baseUrl: "",
            signalEnabled: true
        },
        
        // Fee configs
        defaultFeePercent: 0.001, // 0.1% commission
        defaultSlippagePercent: 0.0005 // 0.05% default base slip
    };
})();
