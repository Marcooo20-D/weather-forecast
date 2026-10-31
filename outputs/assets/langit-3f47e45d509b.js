
(function(){
  'use strict';

  // Retrieve dynamic configuration injected from backend engine
  const config = window.LANGIT_CONFIG || {};

  const prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const freshness = document.querySelector('.freshness-note');
  if (freshness) {
    const issued = Date.parse(config.forecast_issued_at);
    const age = (Date.now()-issued)/3600000;
    if (!Number.isFinite(issued) || age < -1 || age > (config.stale_after_hours || 18)) {
      freshness.classList.add('is-stale');
      freshness.textContent = 'Prakiraan perlu diperiksa pembaruannya. ' + freshness.textContent;
    }
  }
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
    if (prefersReducedMotion) { obj.textContent = end.toFixed(decimals) + suffix; return; }
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
      
      if (suffix === '%' && targetVal > 0 && targetVal < 1) { el.textContent = '<1%'; return; }
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
      if (!svg || !hours.length || svg.clientWidth <= 0) return;
      const w = Math.max(240, svg.clientWidth), h = Math.max(180, svg.clientHeight);
      svg.setAttribute('viewBox', '0 0 ' + w + ' ' + h);
      const model = window.LangitPublicData.chart(hours, activeVar, w, h);
      const strokeColor = {rain:'#06b6d4',temp:'#f97316',humidity:'#a5b4fc',wind:'#34d399'}[activeVar];
      const unit = model.unit;
      const path = points => points.map((point,index) => (index ? 'L ' : 'M ') + point.x.toFixed(1) + ',' + point.y.toFixed(1)).join(' ');
      svg.replaceChildren();
      function element(tag, attributes, content) {
        const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
        Object.entries(attributes).forEach(([name,value]) => node.setAttribute(name,String(value)));
        if (content != null) node.textContent = content;
        svg.appendChild(node);
        return node;
      }
      [model.low, (model.high + model.low)/2, model.high].forEach(value => {
        const y = model.padTop + (model.high-value)/(model.high-model.low)*(model.bottom-model.padTop);
        element('line',{x1:model.padX,y1:y,x2:w-model.padX,y2:y,stroke:'rgba(255,255,255,.12)'});
        element('text',{x:model.padX-8,y:y+4,'text-anchor':'end',fill:'#94a3b8','font-size':10},Math.round(value));
      });
      model.segments.forEach(segment => {
        if (segment.length < 2) return;
        element('path',{class:'curve-fill',d:path(segment)+' L '+segment[segment.length-1].x+','+model.bottom+' L '+segment[0].x+','+model.bottom+' Z',fill:strokeColor,opacity:.08});
        element('path',{class:'curve-line',d:path(segment),stroke:strokeColor,fill:'none'});
      });
      const stride = Math.max(1,Math.ceil(model.points.length / Math.max(2,Math.floor(w/75))));
      model.points.forEach((point,index) => {
        if (index % stride === 0 || index === model.points.length-1) {
          element('text',{class:'curve-label',x:point.x,y:h-8},point.hr.hour);
        }
        if (point.val == null) return;
        const dot = element('circle',{class:'curve-dot',cx:point.x,cy:point.y,r:5,fill:strokeColor,tabindex:0,role:'img',
          'aria-label':point.hr.hour+' WIB: '+window.LangitPublicData.format(point.val,unit)});
        dot.addEventListener('mouseenter',event=>showTooltip(event,point,unit));
        dot.addEventListener('mouseleave',hideTooltip);
        dot.addEventListener('focus',event=>showTooltip(event,point,unit));
        dot.addEventListener('blur',hideTooltip);
        if (index % stride === 0 || index === model.points.length-1) {
          element('text',{class:'curve-value',x:point.x,y:Math.max(15,point.y-12)},window.LangitPublicData.format(point.val,unit));
        }
      });
      const status = container.querySelector('.chart-status');
      if (status) status.textContent = model.points.some(point=>point.val!=null) ? '' : 'Data parameter ini belum tersedia.';
      chips.forEach(chip=>chip.setAttribute('aria-pressed',String(chip.dataset.var===activeVar)));
    }

    function showTooltip(ev, p, unit) {
      if (!tooltip) return;
      const x = p.x;
      const y = p.y;
      
      tooltip.querySelector('.chart-tooltip-time').textContent = 'Pukul ' + p.hr.hour + ' WIB';
      tooltip.querySelector('.chart-tooltip-value').textContent = window.LangitPublicData.format(p.val,unit) + ' · ' + (p.hr.condition || '-');
      
      tooltip.style.opacity = '1';
      tooltip.style.transform = 'translate(' + (x - 70) + 'px, ' + (y - 75) + 'px)';
    }

    function hideTooltip() {
      if (tooltip) tooltip.style.opacity = '0';
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
    document.addEventListener('langit:daychange', drawChart);
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
      container.querySelectorAll('.timeline-node').forEach(button=>button.setAttribute('aria-pressed',String(button===node)));
      
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
        document.dispatchEvent(new Event('langit:daychange'));
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
        if (window.innerWidth < 768 && typeof sheet.showModal === 'function') {
          ev.preventDefault();
          sheet.showModal();
        }
      });
      
      const handle = sheet.querySelector('.sheet-close');
      if (handle) {
        handle.addEventListener('click', () => {
          sheet.close();
        });
      }
    }
  });

  // Static forecast content is available immediately; no simulated telemetry.

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
