# LANGIT Omega Product Reconstruction Report

## DATA ATLAS
| Data Source | Files | Potential Use | Risk |
|---|---:|---|---|
| Advanced/Sentinel | 96 | advanced panel, risk/confidence, exploration |  |
| Ensemble | 30 | confidence, spread, dominant weather |  |
| Forecast per provider | 25 | advanced comparison, source transparency |  |
| Generated HTML | 52 |  |  |
| Map | 23 | map layer and marker/timeline |  |
| Other | 182 |  |  |
| Public API | 9 | primary UI contract |  |
| Raw payload | 27 | debug/archive only | do not expose raw public payload |
| Source status | 34 | data status bar and reliability |  |

## VARIABLE INVENTORY
| Variable | Available | Unit | Missing Rate | Recommended UI Usage | Priority |
|---|---:|---|---:|---|---|
| Hujan | true | % | 0% | utama | P0 |
| Risiko | true | /100 | 0% | utama | P0 |
| Suhu | true | C | 0% | utama | P0 |
| Angin | true | km/jam | 0% | compact | P1 |
| Indeks panas | true | C | 0% | compact | P1 |
| Kelembapan | true | % | 0% | compact | P1 |
| Awan | true | % | 0% | detail | P2 |
| Curah hujan | true | mm | 67% | detail | P2 |
| Dew point | true | C | 67% | disabled | P5 |
| Gust | true | km/jam | 67% | disabled | P5 |
| Jarak pandang | true | m | 67% | disabled | P5 |
| Keandalan | true | % | 67% | disabled | P5 |
| Tekanan | true | hPa | 67% | disabled | P5 |
| UV | true | index | 67% | disabled | P5 |

## DATA HIERARCHY
- P0: condition, temperature, rainProbability, riskScore, windSpeed, updateTime, locationName
- P1: humidity, heatIndex, cloudCover, peakRainHour, safeWindow
- P2: hourlyTrend, dailyTrend, rainAmount, confidence, locationComparison
- P3: variableExplorer, mapLayer, timeline
- P4: source, coverage, pressure, dewPoint, visibility, gust, rawField
- P5: raw payload, debug-only metadata, internal manifests

## COPY REDUCTION REPORT
| Current Text | Location | Problem | New Text | Reason |
|---|---|---|---|---|
| Konsol Cuaca Spasial | legacy/generated HTML | terlalu futuristik | Peta Cuaca | lebih kredibel |
| Command Center | legacy map section | terasa dashboard taktis | Peta Cuaca | fungsi jelas |
| Tactical Weather / Intelligence OS | legacy copy | overclaim | Ringkasan Cuaca | pendek dan faktual |
| real-time | klaim lama bila muncul | data bukan live sensor | Diperbarui | tidak overclaim |
| sensor | klaim lama bila muncul | sumber adalah model/API | sumber data | akurasi istilah |
| Monitoring otomatis keandalan dan tingkat akurasi verifikasi data cuaca. | keandalan_data | terlalu panjang | Keandalan sumber. | copy ringkas |

## INFORMATION ARCHITECTURE FINAL
1. Hero compact
2. Nowcast summary
3. Intelligence strip
4. Dynamic map
5. Forecast timeline
6. Variable explorer
7. Location comparison
8. Advanced data panel
9. Data status

## COMPONENT PLAN
- MainWeatherHero, CurrentConditionPanel, MetricStrip, WeatherMapShell, LayerControl, ForecastTimeline, VariableExplorer, AdaptiveChartPanel, LocationComparison, AdvancedDataDrawer, DataStatusBar, EmptyState, ErrorState, LoadingState.

## MAP DATA PLAN
- Active: risk, rain, temperature, wind speed, humidity, cloud cover.
- Disabled/advanced when missing: pressure, UV, visibility, gust direction vector.
- Data is point-based; map uses markers, halos, local influence fields, and legends. It does not claim national radar.

## RESPONSIVE PLAN
- Desktop: map and side intelligence panel side by side.
- Tablet: stacked map with horizontal controls.
- Mobile: summary first, map full width, chips scroll horizontally, advanced panel collapsed.

## LIMITATION REPORT
- Pressure, dew point, UV, visibility, gust, and wind direction are not consistently available in public data. They are kept disabled or advanced-only.
- Current output is forecast/model data, not live sensor data. UI uses update/freshness language, not real-time claims.
