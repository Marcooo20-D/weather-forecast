# Regional map and meteorology headlines

The campus forecast engine remains the source of point forecasts. The regional
map uses a separate NOAA GFS surface grid retrieved through Open-Meteo's native
`gfs013` bounding-box endpoint. The requested domain is 106.75–109°E,
7.5–6.25°S; returned native coordinates are retained exactly.

## Scientific conventions

- Surface resolution: approximately 13 km. Wind: 10 m; temperature and RH: 2 m.
- Meteorological wind direction is converted to u/v with `u=-s sin(d)` and
  `v=-s cos(d)`. Components are interpolated, preserving the 0/360° boundary.
- Display interpolation is bilinear. Missing values and coordinates outside the
  grid produce no shading or wind flow. Rain is preceding-hour accumulation in mm.
- Animation accelerates display time; it depicts direction and relative speed,
  not a trajectory forecast. Motion can be paused and respects reduced-motion
  preferences. Hidden maps suspend animation.
- Valid time and data retrieval time are distinct. Model run time is not inferred
  from the API's request processing duration.
- Failed refreshes retain valid data for at most 18 hours; campus forecasts
  remain usable when regional data is unavailable.

Source: https://open-meteo.com/en/docs/gfs-api · Data: CC BY 4.0.

## Headlines

Publisher feeds: ANTARA national rolling/top feeds, ANTARA Jabar, and BMKG's
public news listing. Only weather-related headlines, dates, sources and links
are republished. Article bodies and publisher images are not copied.

Filtering excludes unrelated administrative news, wind-power stories, older
articles and foreign-only events. Canonical publisher URLs are deduplicated.
Date-only BMKG records retain date-only precision. Regional labels follow the
headline's named geography. A dated cache is used during source failure.

The hourly headline workflow shares production concurrency with forecast
publication to avoid competing pushes. It publishes only the headline JSON;
forecast pages and numerical products continue through the full production job.

## Repository layout

`build_weather_site.py` is the current entry point. Historical builders and
one-off repair scripts live in `legacy/`. Existing commit history and scientific archives retain their provenance.
