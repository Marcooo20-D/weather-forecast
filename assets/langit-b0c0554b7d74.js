
(function(){
  'use strict';

  // Retrieve dynamic configuration injected from backend engine
  const config = window.LANGIT_CONFIG || {};

  // Safe SessionStorage wrapper to prevent incognito/private mode crashes
  const safeStorage = {
    getItem(key) {
      try { return sessionStorage.getItem(key); } catch (e) { return null; }
    },
    setItem(key, value) {
      try { sessionStorage.setItem(key, value); } catch (e) {}
    }
  };

  const prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  let initialLayer = 'rain';
  const cond = (config.condition || '').toLowerCase();
  if (cond.includes('hujan') || cond.includes('gerimis') || cond.includes('rain') || cond.includes('drizzle')) {
    initialLayer = 'rain';
  } else if (cond.includes('panas') || cond.includes('cerah') || cond.includes('clear') || cond.includes('hot') || cond.includes('sunny')) {
    initialLayer = 'temp';
  } else if (cond.includes('angin') || cond.includes('wind') || cond.includes('storm')) {
    initialLayer = 'wind';
  } else if (cond.includes('awan') || cond.includes('mendung') || cond.includes('cloudy') || cond.includes('overcast')) {
    initialLayer = 'cloud';
  } else if (cond.includes('lembap') || cond.includes('kabut') || cond.includes('fog') || cond.includes('mist') || cond.includes('humidity')) {
    initialLayer = 'humidity';
  }

  // State Management
  const state = {
    activeDayIndex: 0,
    activeLayer: initialLayer,
    hourlyData: null
  };

  // --- Scroll & Reveal System ---
  const revealEls = document.querySelectorAll('.reveal');
  if ('IntersectionObserver' in window) {
    const obs = new IntersectionObserver(function(entries) {
      entries.forEach(function(e) {
        if (e.isIntersecting) {
          e.target.classList.add('visible');
          obs.unobserve(e.target);
        }
      });
    }, { threshold: 0.05, rootMargin: '0px 0px -20px 0px' });
    revealEls.forEach(function(el) { obs.observe(el); });
  } else {
    revealEls.forEach(function(el) { el.classList.add('visible'); });
  }

  // --- Smooth Numerical Counting Transition ---
  function animateValue(obj, start, end, duration, suffix = '', decimals = 0) {
    if (!obj) return;
    if (obj._animId) {
      window.cancelAnimationFrame(obj._animId);
    }
    let startTimestamp = null;
    const step = (timestamp) => {
      if (!startTimestamp) startTimestamp = timestamp;
      const progress = Math.min((timestamp - startTimestamp) / duration, 1);
      const val = progress * (end - start) + start;
      obj.innerHTML = val.toFixed(decimals) + suffix;
      if (progress < 1) {
        obj._animId = window.requestAnimationFrame(step);
      } else {
        obj._animId = null;
      }
    };
    obj._animId = window.requestAnimationFrame(step);
  }

  // Parses value from element content to animate
  function triggerCounters(panel) {
    if (!panel) return;
    panel.querySelectorAll('[data-animate-value]').forEach(el => {
      const targetVal = parseFloat(el.dataset.animateValue);
      const suffix = el.dataset.animateSuffix || '';
      const decimals = parseInt(el.dataset.animateDecimals || '0');
      if (!Number.isFinite(targetVal) || el.textContent.trim().startsWith('—')) return;
      
      const currentVal = parseFloat(el.textContent) || 0;
      animateValue(el, currentVal, targetVal, 800, suffix, decimals);
    });
  }

  // --- Volumetric Canvas Atmospheric Particle System ---
  const atmoCanvas = document.getElementById('atmo-canvas');
  let atmoCtx = null;
  let particles = [];
  let atmoWidth = 0, atmoHeight = 0;
  let animId = null;

  let isAtmoVisible = true;
  if (atmoCanvas && !prefersReducedMotion) {
    atmoCtx = atmoCanvas.getContext('2d');
    resizeAtmo();
    initParticles();
    
    if ('IntersectionObserver' in window) {
      const observer = new IntersectionObserver((entries) => {
        entries.forEach(entry => {
          isAtmoVisible = entry.isIntersecting;
          if (isAtmoVisible && !animId) {
            animLoop();
          } else if (!isAtmoVisible && animId) {
            cancelAnimationFrame(animId);
            animId = null;
          }
        });
      }, { threshold: 0.01 });
      observer.observe(atmoCanvas);
    } else {
      animLoop();
    }
  }

  function resizeAtmo() {
    atmoWidth = atmoCanvas.width = window.innerWidth;
    atmoHeight = atmoCanvas.height = window.innerHeight;
  }

  // Viewport-aware particle count for performance
  function getParticleCount(layer) {
    const baseCounts = { rain: 80, cloud: 15, wind: 40, temp: 40, humidity: 30 };
    const base = baseCounts[layer] || 40;
    const w = window.innerWidth;
    if (w <= 479) return Math.round(base * 0.2);
    if (w <= 767) return Math.round(base * 0.35);
    if (w <= 1023) return Math.round(base * 0.6);
    return base;
  }

  function initParticles() {
    particles = [];
    const count = getParticleCount(state.activeLayer);

    for (let i = 0; i < count; i++) {
      particles.push(createParticle());
    }
  }

  function createParticle(initBottom = false) {
    const p = {
      x: Math.random() * atmoWidth,
      y: initBottom ? (atmoHeight + 50) : (Math.random() * (atmoHeight + 100) - 50),
      size: 0,
      vx: 0,
      vy: 0,
      alpha: Math.random() * 0.4 + 0.1,
      color: '#ffffff'
    };

    if (state.activeLayer === 'rain') {
      p.size = Math.random() * 1.5 + 0.5;
      p.vy = Math.random() * 12 + 10;
      p.vx = -1.5 - Math.random() * 1;
      p.color = '100, 182, 212';
    } else if (state.activeLayer === 'wind') {
      p.size = Math.random() * 1.2 + 0.5;
      p.vx = Math.random() * 4 + 2;
      p.vy = Math.sin(p.x * 0.005) * 0.5;
      p.color = '168, 196, 224';
      p.trail = [];
    } else if (state.activeLayer === 'cloud') {
      p.size = Math.random() * 80 + 50;
      p.vx = Math.random() * 0.2 + 0.05;
      p.vy = 0;
      p.color = '148, 190, 235';
      p.alpha = Math.random() * 0.08 + 0.02;
    } else if (state.activeLayer === 'temp') {
      p.size = Math.random() * 15 + 5;
      p.vy = -(Math.random() * 1.5 + 0.5);
      p.vx = (Math.random() - 0.5) * 0.5;
      p.color = '249, 115, 22';
      p.alpha = Math.random() * 0.15 + 0.05;
    } else if (state.activeLayer === 'humidity') {
      p.size = Math.random() * 40 + 20;
      p.vy = -(Math.random() * 0.8 + 0.2);
      p.vx = (Math.random() - 0.5) * 0.2;
      p.color = '99, 102, 241';
      p.alpha = Math.random() * 0.1 + 0.02;
    }
    return p;
  }

  function animLoop() {
    if (!atmoCtx || !isAtmoVisible) {
      animId = null;
      return;
    }
    atmoCtx.clearRect(0, 0, atmoWidth, atmoHeight);

    particles.forEach(p => {
      // Move particle
      p.x += p.vx;
      p.y += p.vy;

      if (state.activeLayer === 'wind') {
        p.vy = Math.sin(p.x * 0.005) * 0.4;
        p.trail.push({ x: p.x, y: p.y });
        if (p.trail.length > 15) p.trail.shift();
      }

      // Draw particle
      atmoCtx.beginPath();
      if (state.activeLayer === 'rain') {
        atmoCtx.strokeStyle = 'rgba(' + p.color + ',' + p.alpha + ')';
        atmoCtx.lineWidth = p.size;
        atmoCtx.moveTo(p.x, p.y);
        atmoCtx.lineTo(p.x + p.vx * 1.5, p.y + p.vy * 1.5);
        atmoCtx.stroke();
      } else if (state.activeLayer === 'wind') {
        if (p.trail.length > 1) {
          atmoCtx.beginPath();
          atmoCtx.moveTo(p.trail[0].x, p.trail[0].y);
          for (let k = 1; k < p.trail.length; k++) {
            atmoCtx.lineTo(p.trail[k].x, p.trail[k].y);
          }
          atmoCtx.strokeStyle = 'rgba(' + p.color + ',' + p.alpha + ')';
          atmoCtx.lineWidth = p.size;
          atmoCtx.stroke();
        }
      } else if (state.activeLayer === 'cloud' || state.activeLayer === 'temp' || state.activeLayer === 'humidity') {
        const rad = atmoCtx.createRadialGradient(p.x, p.y, 0, p.x, p.y, p.size);
        rad.addColorStop(0, 'rgba(' + p.color + ',' + p.alpha + ')');
        rad.addColorStop(1, 'rgba(' + p.color + ',0)');
        atmoCtx.fillStyle = rad;
        atmoCtx.arc(p.x, p.y, p.size, 0, Math.PI * 2);
        atmoCtx.fill();
      }

      // Boundary wraps
      if (state.activeLayer === 'rain') {
        if (p.y > atmoHeight + 20 || p.x < -20) {
          Object.assign(p, createParticle());
          p.y = -20;
        }
      } else if (state.activeLayer === 'wind' || state.activeLayer === 'cloud') {
        if (p.x > atmoWidth + p.size) {
          Object.assign(p, createParticle());
          p.x = -p.size;
        }
      } else if (state.activeLayer === 'temp' || state.activeLayer === 'humidity') {
        if (p.y < -p.size || p.x < -p.size || p.x > atmoWidth + p.size) {
          Object.assign(p, createParticle(true));
        }
      }
    });

    animId = window.requestAnimationFrame(animLoop);
  }

  // --- Dynamic Color Shifting Body Themes ---
  function shiftAtmosphereTheme(relativeDay) {
    document.body.className = '';
    const day = relativeDay.toLowerCase();
    
    // Choose theme based on time of day context or weather risk
    if (day.includes('hari') || day.includes('today')) {
      document.body.classList.add('theme-noon'); // Noon cyan for today
    } else if (day.includes('besok') || day.includes('tomorrow')) {
      document.body.classList.add('theme-dusk'); // Violet dusk for tomorrow
    } else {
      document.body.classList.add('theme-dawn'); // Orange dawn for lusa
    }
  }

  // --- Interactive Curved SVG Bezier Chart Switcher ---
  const chartContainers = document.querySelectorAll('.chart-wrapper');
  chartContainers.forEach(container => {
    const svgCont = container.querySelector('.chart-svg-container');
    if (!svgCont) return;
    const rawData = svgCont.dataset.points;
    if (!rawData) return;
    let hours = [];
    try {
      hours = JSON.parse(rawData);
    } catch(err) {
      return;
    }
    
    let activeVar = 'rain'; // rain, temp, humidity, wind
    const chips = container.querySelectorAll('.chart-chip');
    const svg = container.querySelector('.rain-curve');
    const tooltip = container.querySelector('.chart-tooltip');
    
    function drawChart() {
      if (!svg || !hours.length) return;
      const w = svg.clientWidth || 900;
      const h = svg.clientHeight || 200;
      const padX = w < 400 ? 25 : w < 700 ? 35 : 50;
      const padTop = w < 400 ? 20 : 30;
      const padBot = w < 400 ? 30 : 40;
      const innerW = w - 2 * padX;
      const innerH = h - padTop - padBot;
      const n = hours.length;

      // Determine Variable Metadata
      let getter = x => 0;
      let unit = '';
      let strokeColor = '#06b6d4';
      let fillGradientId = 'noonChartGrad';
      let title = '';

      if (activeVar === 'rain') {
        getter = x => parseFloat(x.rain_probability || 0);
        unit = '%';
        strokeColor = '#06b6d4'; // Cyan
        fillGradientId = 'rainChartGrad';
      } else if (activeVar === 'temp') {
        getter = x => parseFloat(x.temp_c || 0);
        unit = '°C';
        strokeColor = '#f97316'; // Orange
        fillGradientId = 'tempChartGrad';
      } else if (activeVar === 'humidity') {
        getter = x => parseFloat(x.humidity_pct || 0);
        unit = '%';
        strokeColor = '#6366f1'; // Indigo
        fillGradientId = 'humidityChartGrad';
      } else if (activeVar === 'wind') {
        getter = x => parseFloat(x.wind_kmh || 0);
        unit = ' km/j';
        strokeColor = '#10b981'; // Green
        fillGradientId = 'windChartGrad';
      }

      const vals = hours.map(getter);
      const minVal = activeVar === 'temp' ? Math.min(...vals, 15) - 2 : 0;
      const maxVal = Math.max(...vals, activeVar === 'temp' ? 30 : 100) || 1;
      const valRange = maxVal - minVal;

      const points = hours.map((hr, i) => {
        const x = padX + (i / Math.max(1, n - 1)) * innerW;
        const y = padTop + innerH - ((getter(hr) - minVal) / valRange) * innerH;
        return { x, y, hr, val: getter(hr) };
      });

      // SVG path helpers
      function bezierPath(pts) {
        if (pts.length < 2) return '';
        let d = 'M ' + pts[0].x.toFixed(1) + ',' + pts[0].y.toFixed(1);
        if (pts.length === 2) {
          d += ' L ' + pts[1].x.toFixed(1) + ',' + pts[1].y.toFixed(1);
          return d;
        }
        for (let i = 0; i < pts.length - 1; i++) {
          const p0 = pts[Math.max(0, i - 1)];
          const p1 = pts[i];
          const p2 = pts[Math.min(pts.length - 1, i + 1)];
          const p3 = pts[Math.min(pts.length - 1, i + 2)];
          
          const cp1x = p1.x + (p2.x - p0.x) / 6;
          const cp1y = p1.y + (p2.y - p0.y) / 6;
          const cp2x = p2.x - (p3.x - p1.x) / 6;
          const cp2y = p2.y - (p3.y - p1.y) / 6;
          
          d += ' C ' + cp1x.toFixed(1) + ',' + cp1y.toFixed(1) + ' ' +
               cp2x.toFixed(1) + ',' + cp2y.toFixed(1) + ' ' +
               p2.x.toFixed(1) + ',' + p2.y.toFixed(1);
        }
        return d;
      }

      const linePathD = bezierPath(points);
      const fillPathD = linePathD + ' L ' + points[points.length-1].x.toFixed(1) + ',' + (h - padBot).toFixed(1) + ' L ' + points[0].x.toFixed(1) + ',' + (h - padBot).toFixed(1) + ' Z';

      // Draw SVG Elements
      svg.innerHTML = `
        <defs>
          <linearGradient id="rainChartGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="#06b6d4" stop-opacity="0.25"/>
            <stop offset="100%" stop-color="#06b6d4" stop-opacity="0"/>
          </linearGradient>
          <linearGradient id="tempChartGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="#f97316" stop-opacity="0.25"/>
            <stop offset="100%" stop-color="#f97316" stop-opacity="0"/>
          </linearGradient>
          <linearGradient id="humidityChartGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="#6366f1" stop-opacity="0.25"/>
            <stop offset="100%" stop-color="#6366f1" stop-opacity="0"/>
          </linearGradient>
          <linearGradient id="windChartGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="#10b981" stop-opacity="0.25"/>
            <stop offset="100%" stop-color="#10b981" stop-opacity="0"/>
          </linearGradient>
        </defs>
        
        <!-- Y Grid Lines -->
        <line x1="${padX}" y1="${padTop}" x2="${w - padX}" y2="${padTop}" stroke="rgba(255,255,255,0.03)" stroke-width="1"/>
        <line x1="${padX}" y1="${padTop + innerH/2}" x2="${w - padX}" y2="${padTop + innerH/2}" stroke="rgba(255,255,255,0.03)" stroke-width="1"/>
        <line x1="${padX}" y1="${h - padBot}" x2="${w - padX}" y2="${h - padBot}" stroke="rgba(255,255,255,0.08)" stroke-width="1"/>
        
        <!-- Main Area Grads & Line -->
        <path class="curve-fill" d="${fillPathD}" fill="url(#${fillGradientId})"/>
        <path class="curve-line" d="${linePathD}" stroke="${strokeColor}"/>
      `;

      // Draw interactive dots and text labels
      points.forEach(p => {
        const dot = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
        dot.setAttribute('class', 'curve-dot');
        dot.setAttribute('cx', p.x.toFixed(1));
        dot.setAttribute('cy', p.y.toFixed(1));
        dot.setAttribute('r', '5');
        dot.setAttribute('fill', strokeColor);
        dot.addEventListener('mouseenter', (ev) => showTooltip(ev, p, unit));
        dot.addEventListener('mouseleave', hideTooltip);
        svg.appendChild(dot);

        const label = document.createElementNS('http://www.w3.org/2000/svg', 'text');
        label.setAttribute('class', 'curve-label');
        label.setAttribute('x', p.x.toFixed(1));
        label.setAttribute('y', (h - 8).toString());
        label.textContent = p.hr.hour;
        svg.appendChild(label);

        // Conditional numeric displays
        if (p.val > 0 || activeVar === 'temp') {
          const valTxt = document.createElementNS('http://www.w3.org/2000/svg', 'text');
          valTxt.setAttribute('class', 'curve-value');
          valTxt.setAttribute('x', p.x.toFixed(1));
          valTxt.setAttribute('y', Math.max(15, p.y - 12).toFixed(1));
          valTxt.textContent = Math.round(p.val) + unit;
          svg.appendChild(valTxt);
        }
      });
    }

    function showTooltip(ev, p, unit) {
      if (!tooltip) return;
      const x = p.x;
      const y = p.y;
      
      tooltip.querySelector('.chart-tooltip-time').textContent = 'Pukul ' + p.hr.hour + ' WIB';
      tooltip.querySelector('.chart-tooltip-value').textContent = p.val.toFixed(decimalsForVar()) + unit + ' · ' + (p.hr.condition || '-');
      
      tooltip.style.opacity = '1';
      tooltip.style.transform = 'translate(' + (x - 70) + 'px, ' + (y - 75) + 'px)';
    }

    function hideTooltip() {
      if (tooltip) tooltip.style.opacity = '0';
    }

    function decimalsForVar() {
      return activeVar === 'temp' ? 1 : 0;
    }

    chips.forEach(chip => {
      chip.addEventListener('click', () => {
        chips.forEach(c => c.classList.remove('active'));
        chip.classList.add('active');
        activeVar = chip.dataset.var;
        
        // Sync particle engine state to the matching visualization
        if (activeVar === 'rain') state.activeLayer = 'rain';
        else if (activeVar === 'temp') state.activeLayer = 'temp';
        else if (activeVar === 'humidity') state.activeLayer = 'humidity';
        else if (activeVar === 'wind') state.activeLayer = 'wind';
        initParticles();

        // Sync active class on layer control buttons
        document.querySelectorAll('.layer-btn').forEach(b => {
          if (b.dataset.layer === state.activeLayer) {
            b.classList.add('active');
          } else {
            b.classList.remove('active');
          }
        });

        drawChart();
      });
    });

    drawChart();
    let chartResizeTimeout;
    window.addEventListener('resize', () => {
      clearTimeout(chartResizeTimeout);
      chartResizeTimeout = setTimeout(drawChart, 100);
    }, { passive: true });
  });

  // --- Dynamic Weather Stepper Timeline Navigation ---
  const timelineNodes = document.querySelectorAll('.timeline-node');
  const fillBar = document.querySelector('.timeline-progress-fill');
  
  function updateTimelineProgress() {
    if (!timelineNodes.length || !fillBar) return;
    let activeIdx = 0;
    timelineNodes.forEach((node, idx) => {
      if (node.classList.contains('active')) {
        activeIdx = idx;
      }
    });
    
    const pct = (activeIdx / (timelineNodes.length - 1)) * 100;
    fillBar.style.width = pct + '%';
  }

  timelineNodes.forEach((node, index) => {
    node.addEventListener('click', () => {
      var container = node.closest('.day-tabs-container');
      if (!container) return;
      
      container.querySelectorAll('.timeline-node').forEach(t => t.classList.remove('active'));
      node.classList.add('active');
      
      // Update color scheme of the body gradient
      const relLabel = node.querySelector('.timeline-node-label').textContent;
      shiftAtmosphereTheme(relLabel);
      
      // Hide and show day panels
      const targetId = node.dataset.target;
      const targetPanel = document.getElementById(targetId);
      if (targetPanel) {
        document.querySelectorAll('.day-panel').forEach(p => p.classList.remove('active'));
        targetPanel.classList.add('active');
        triggerCounters(targetPanel);
      }
      
      // Notify map frames to update layout when tabs change
      document.querySelectorAll('.map-frame').forEach(iframe => {
        if (iframe.contentWindow) {
          iframe.contentWindow.postMessage({ type: 'invalidateSize' }, window.location.origin);
        }
      });
      
      updateTimelineProgress();
    });
  });

  updateTimelineProgress();

  // --- Dynamic Particle Layer Switchers ---
  document.querySelectorAll('.layer-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.layer-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      state.activeLayer = btn.dataset.layer;
      initParticles();

      // Sync active chart parameter chip
      document.querySelectorAll('.chart-wrapper').forEach(wrapper => {
        const chips = wrapper.querySelectorAll('.chart-chip');
        chips.forEach(chip => {
          if (chip.dataset.var === state.activeLayer) {
            if (!chip.classList.contains('active')) {
              chip.click();
            }
          }
        });
      });

      // Sync the embedded map iframe layer
      const mapFrame = document.querySelector('.map-frame');
      if (mapFrame && mapFrame.contentWindow) {
        mapFrame.contentWindow.postMessage({ type: 'switchLayer', layer: state.activeLayer }, window.location.origin);
      }
    });
  });

  // Align active classes on DOM Load
  document.addEventListener('DOMContentLoaded', () => {
    // Update active class on layer buttons
    document.querySelectorAll('.layer-btn').forEach(btn => {
      if (btn.dataset.layer === state.activeLayer) {
        btn.classList.add('active');
      } else {
        btn.classList.remove('active');
      }
    });

    // Update active class on chart chips
    document.querySelectorAll('.chart-wrapper').forEach(wrapper => {
      const chips = wrapper.querySelectorAll('.chart-chip');
      chips.forEach(chip => {
        if (chip.dataset.var === state.activeLayer) {
          chip.classList.add('active');
        } else {
          chip.classList.remove('active');
        }
      });
    });
  });

  // --- Mobile Bottom Sheet Modal ---
  document.querySelectorAll('.day-panel').forEach((panel) => {
    const sheet = panel.querySelector('.mobile-bottom-sheet');
    const sheetTrigger = panel.querySelector('.hourly-toggle');
    
    if (sheet && sheetTrigger) {
      sheetTrigger.addEventListener('click', (ev) => {
        // If mobile, prevent default details collapse and show sliding bottom sheet instead
        if (window.innerWidth < 768) {
          ev.preventDefault();
          sheet.classList.add('open');
        }
      });
      
      const handle = sheet.querySelector('.mobile-bottom-sheet-handle');
      if (handle) {
        handle.addEventListener('click', () => {
          sheet.classList.remove('open');
        });
      }
    }
  });

  // --- Apple Vision Pro Spatial Coordinates Zoom Scanner ---
  function runSpatialZoomScanner() {
    const isMainPage = document.querySelector('.hero') !== null;
    const sessionKey = 'langit_v65_spatial_scanner_run';
    
    if (!isMainPage || safeStorage.getItem(sessionKey) || prefersReducedMotion) {
      // Clean up scanner markup and display page immediately if already run or reduced motion
      const overlay = document.getElementById('spatial-overlay');
      if (overlay) overlay.remove();
      return;
    }

    // Insert overlay markup dynamically if not in HTML
    let overlay = document.getElementById('spatial-overlay');
    if (!overlay) {
      const activeLocName = config.location_name || 'ITB Bandung';
      overlay = document.createElement('div');
      overlay.id = 'spatial-overlay';
      overlay.innerHTML = `
        <canvas id="spatial-canvas"></canvas>
        <div class="spatial-hud">
          <div class="spatial-title">MENYIAPKAN DATA CUACA</div>
          <div class="spatial-status" id="spatial-status">MEMUAT DATA PRAKIRAAN...</div>
        </div>
        <button class="spatial-skip">Lewati</button>
      `;
      document.body.appendChild(overlay);
    }

    const canvas = document.getElementById('spatial-canvas');
    const statusText = document.getElementById('spatial-status');
    const skipBtn = overlay.querySelector('.spatial-skip');
    
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    let width = canvas.width = window.innerWidth;
    let height = canvas.height = window.innerHeight;
    
    window.addEventListener('resize', () => {
      width = canvas.width = window.innerWidth;
      height = canvas.height = window.innerHeight;
    }, { passive: true });

    let scanProgress = 0;
    let frame = 0;
    
    const telemetrySteps = [
      { p: 0.0, txt: 'Menghubungkan ke satelit cuaca...' },
      { p: 0.2, txt: 'Telemetri aman. Memetakan orbit...' },
      { p: 0.45, txt: 'Menginisialisasi pencarian koordinat wilayah Indonesia...' },
      { p: 0.7, txt: 'Memindai wilayah: ' + (config.location_name || 'ITB').toUpperCase() },
      { p: 0.9, txt: 'Koordinat terkunci: Kampus ' + (config.location_name || 'ITB').toUpperCase() }
    ];

    let requestID = null;

    function drawScanner() {
      frame++;
      scanProgress += 0.014;
      if (scanProgress >= 1.0) scanProgress = 1.0;

      // Draw High Tech background grid
      ctx.fillStyle = '#030712';
      ctx.fillRect(0, 0, width, height);

      // Star particles
      ctx.fillStyle = 'rgba(255, 255, 255, 0.2)';
      for (let i = 0; i < 50; i++) {
        const sx = (Math.sin(i * 123.4) * 0.5 + 0.5) * width;
        const sy = (Math.cos(i * 567.8) * 0.5 + 0.5) * height;
        ctx.fillRect(sx, sy, 1.5, 1.5);
      }

      // Globe Grid sequence
      const centerX = width / 2;
      const centerY = height / 2;
      const radiusBase = Math.min(width, height) * 0.35;
      
      // Zoom factor increases over time
      const zoom = 1 + Math.pow(scanProgress, 2.5) * 8;
      const currentRadius = radiusBase * zoom;

      // Update HUD telemetry
      const step = telemetrySteps.find((s, idx) => {
        const next = telemetrySteps[idx + 1];
        return scanProgress >= s.p && (!next || scanProgress < next.p);
      });
      if (step) {
        statusText.textContent = step.txt;
      }

      ctx.strokeStyle = 'rgba(6, 182, 212, ' + (1 - scanProgress * 0.8) + ')';
      ctx.lineWidth = 1;

      if (scanProgress < 0.7) {
        // Draw rotating sphere wireframe (Globe Search)
        const rotY = frame * 0.012;
        ctx.beginPath();
        ctx.arc(centerX, centerY, currentRadius, 0, Math.PI * 2);
        ctx.stroke();

        // Latitude / Longitude lines
        const lines = 8;
        for (let j = 0; j < lines; j++) {
          // Horizontal slices
          const yOffset = Math.sin((j / lines - 0.5) * Math.PI) * currentRadius;
          const sliceRad = Math.cos((j / lines - 0.5) * Math.PI) * currentRadius;
          ctx.beginPath();
          ctx.ellipse(centerX, centerY + yOffset, sliceRad, sliceRad * 0.25, 0, 0, Math.PI * 2);
          ctx.stroke();

          // Vertical slices
          ctx.beginPath();
          ctx.ellipse(centerX, centerY, currentRadius * 0.25, currentRadius, rotY + (j / lines) * Math.PI, 0, Math.PI * 2);
          ctx.stroke();
        }

        // Radar target sweeps
        ctx.beginPath();
        ctx.moveTo(centerX, centerY);
        ctx.arc(centerX, centerY, currentRadius, rotY, rotY + 0.5);
        ctx.fillStyle = 'rgba(6, 182, 212, 0.03)';
        ctx.fill();
      } else {
        // Spatial coordinates mapping focus
        ctx.lineWidth = 1.5;
        const opacity = 1 - (scanProgress - 0.7) / 0.3;
        ctx.strokeStyle = 'rgba(16, 185, 129, ' + opacity + ')'; // green lock-on color

        // Focused radar rings
        ctx.beginPath();
        ctx.arc(centerX, centerY, 50, 0, Math.PI * 2);
        ctx.stroke();

        ctx.beginPath();
        ctx.arc(centerX, centerY, 150 + Math.sin(frame * 0.1) * 8, 0, Math.PI * 2);
        ctx.stroke();

        // Crosshairs
        ctx.beginPath();
        ctx.moveTo(centerX - 250, centerY);
        ctx.lineTo(centerX - 30, centerY);
        ctx.moveTo(centerX + 30, centerY);
        ctx.lineTo(centerX + 250, centerY);
        ctx.moveTo(centerX, centerY - 200);
        ctx.lineTo(centerX, centerY - 30);
        ctx.moveTo(centerX, centerY + 30);
        ctx.lineTo(centerX, centerY + 200);
        ctx.stroke();

        // Retrieve coordinates from window config
        const lat = config.latitude !== undefined && config.latitude !== null ? config.latitude : -6.8830;
        const lon = config.longitude !== undefined && config.longitude !== null ? config.longitude : 107.6130;
        
        const alt = config.altitude !== undefined && config.altitude !== null ? config.altitude : '832';

        const latSign = lat < 0 ? 'S' : 'N';
        const lonSign = lon < 0 ? 'W' : 'E';

        ctx.fillStyle = 'rgba(255, 255, 255, ' + opacity * 0.8 + ')';
        ctx.font = '10px monospace';
        ctx.fillText('LAT: ' + Math.abs(lat).toFixed(4) + '° ' + latSign, centerX + 60, centerY - 40);
        ctx.fillText('LON: ' + Math.abs(lon).toFixed(4) + '° ' + lonSign, centerX + 60, centerY - 20);
        ctx.fillText('ALT: ' + alt + 'm [MSL]', centerX + 60, centerY);
        ctx.fillText('BEARING: 122.4° SE', centerX + 60, centerY + 20);
      }

      if (scanProgress < 1.0) {
        requestID = window.requestAnimationFrame(drawScanner);
      } else {
        closeScanner();
      }
    }

    function closeScanner() {
      if (requestID) window.cancelAnimationFrame(requestID);
      safeStorage.setItem(sessionKey, 'true');
      overlay.style.opacity = '0';
      overlay.style.transform = 'scale(1.05)';
      setTimeout(() => {
        overlay.remove();
        // Trigger page counter reveals
        const activePanel = document.querySelector('.day-panel.active');
        if (activePanel) triggerCounters(activePanel);
      }, 800);
    }

    skipBtn.addEventListener('click', closeScanner);
    drawScanner();
  }

  // Auto-run Spatial Scan overlay sequence on launch
  runSpatialZoomScanner();

  // --- Hamburger Menu Toggle ---
  const navToggle = document.querySelector('.nav-toggle');
  const navLinks = document.querySelector('.nav-links');
  if (navToggle && navLinks) {
    navToggle.addEventListener('click', () => {
      const isOpen = navLinks.classList.toggle('open');
      navToggle.setAttribute('aria-expanded', isOpen.toString());
    });
    // Close menu when clicking a nav link
    navLinks.querySelectorAll('.nav-link').forEach(link => {
      link.addEventListener('click', () => {
        navLinks.classList.remove('open');
        navToggle.setAttribute('aria-expanded', 'false');
      });
    });
    // Close menu when clicking outside
    document.addEventListener('click', (e) => {
      if (!navToggle.contains(e.target) && !navLinks.contains(e.target)) {
        navLinks.classList.remove('open');
        navToggle.setAttribute('aria-expanded', 'false');
      }
    });
  }

  // --- Debounced Resize Handler ---
  let resizeTimer;
  function handleResize() {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      // Recalculate atmospheric canvas
      if (atmoCanvas && atmoCtx) {
        resizeAtmo();
        initParticles();
      }
      // Redraw all charts
      document.querySelectorAll('.chart-wrapper').forEach(wrapper => {
        const chips = wrapper.querySelectorAll('.chart-chip');
        const activeChip = wrapper.querySelector('.chart-chip.active');
        if (activeChip) activeChip.click();
      });
      // Close mobile nav if viewport expanded
      if (window.innerWidth >= 768 && navLinks) {
        navLinks.classList.remove('open');
        if (navToggle) navToggle.setAttribute('aria-expanded', 'false');
      }
    }, 150);
  }
  window.addEventListener('resize', handleResize, { passive: true });

  // --- Orientation Change Handler ---
  window.addEventListener('orientationchange', () => {
    setTimeout(() => {
      if (atmoCanvas && atmoCtx) {
        resizeAtmo();
        initParticles();
      }
      // Notify embedded map iframe to invalidate size
      const mapFrame = document.querySelector('.map-frame');
      if (mapFrame && mapFrame.contentWindow) {
        mapFrame.contentWindow.postMessage({ type: 'invalidateSize' }, window.location.origin);
      }
    }, 300);
  });

})();
