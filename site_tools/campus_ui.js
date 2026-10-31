/* LANGIT Campus interactions. Forecast numbers are supplied by public_data.js. */
(function(){
  'use strict';
  const config=window.LANGIT_CONFIG||{};
  const freshness=document.querySelector('.freshness-note');
  if(freshness){
    const issued=Date.parse(config.forecast_issued_at),age=(Date.now()-issued)/3600000;
    if(!Number.isFinite(issued)||age < -1||age > (config.stale_after_hours||18)){
      freshness.classList.add('is-stale');
      freshness.textContent='Prakiraan perlu diperiksa pembaruannya. '+freshness.textContent;
    }
  }

  const menu=document.querySelector('.nav-links'),toggle=document.querySelector('.nav-toggle');
  function closeMenu(){if(menu&&toggle){menu.classList.remove('open');toggle.setAttribute('aria-expanded','false');}}
  if(menu&&toggle){
    toggle.addEventListener('click',()=>{const open=menu.classList.toggle('open');toggle.setAttribute('aria-expanded',String(open));});
    menu.querySelectorAll('a').forEach(link=>link.addEventListener('click',closeMenu));
    document.addEventListener('click',event=>{if(!menu.contains(event.target)&&!toggle.contains(event.target))closeMenu();});
    document.addEventListener('keydown',event=>{if(event.key==='Escape'&&menu.classList.contains('open')){closeMenu();toggle.focus();}});
  }
  document.querySelectorAll('.campus-switch').forEach(switcher=>{
    document.addEventListener('click',event=>{if(!switcher.contains(event.target))switcher.open=false;});
    switcher.addEventListener('keydown',event=>{if(event.key==='Escape'){switcher.open=false;switcher.querySelector('summary').focus();}});
  });

  document.querySelectorAll('.timeline-node').forEach(node=>node.addEventListener('click',()=>{
    const target=document.getElementById(node.dataset.target),group=node.closest('.day-tabs-container');
    if(!target||!group)return;
    group.querySelectorAll('.timeline-node').forEach(button=>{button.classList.remove('active');button.setAttribute('aria-pressed','false');});
    node.classList.add('active');node.setAttribute('aria-pressed','true');
    document.querySelectorAll('.day-panel').forEach(panel=>panel.classList.remove('active'));
    target.classList.add('active');document.dispatchEvent(new Event('langit:daychange'));
    document.querySelectorAll('.map-frame').forEach(frame=>frame.contentWindow?.postMessage({type:'invalidateSize'},window.location.origin));
  }));

  function setSharedLayer(layer){
    document.querySelectorAll('.layer-btn').forEach(button=>{
      const active=button.dataset.layer===layer;
      button.classList[active?'add':'remove']('active');button.setAttribute('aria-pressed',String(active));
    });
    document.querySelectorAll('.map-frame').forEach(frame=>frame.contentWindow?.postMessage({type:'switchLayer',layer},window.location.origin));
    const event=new Event('langit:layerchange');event.layer=layer;document.dispatchEvent(event);
  }

  document.querySelectorAll('.chart-wrapper').forEach(container=>{
    const holder=container.querySelector('.chart-svg-container'),svg=container.querySelector('.rain-curve');
    if(!holder||!svg)return;
    let hours=[];try{hours=JSON.parse(holder.dataset.points||'[]');}catch{return;}
    const chips=container.querySelectorAll('.chart-chip'),tooltip=container.querySelector('.chart-tooltip');
    let variable='rain';
    const colors={rain:'#0868ab',temp:'#b36726',humidity:'#7163ad',wind:'#18775f'};
    function show(point,unit){
      if(!tooltip)return;
      tooltip.querySelector('.chart-tooltip-time').textContent='Pukul '+point.hr.hour+' WIB';
      tooltip.querySelector('.chart-tooltip-value').textContent=window.LangitPublicData.format(point.val,unit)+' · '+(point.hr.condition||'—');
      const left=Math.max(0,Math.min(point.x-80,holder.clientWidth-220));
      tooltip.style.opacity='1';tooltip.style.transform='translate('+left+'px, '+Math.max(0,point.y-70)+'px)';
    }
    function hide(){if(tooltip)tooltip.style.opacity='0';}
    function draw(){
      if(!hours.length||svg.clientWidth<=0)return;
      const w=Math.max(240,svg.clientWidth),h=Math.max(180,svg.clientHeight);
      svg.setAttribute('viewBox','0 0 '+w+' '+h);
      const model=window.LangitPublicData.chart(hours,variable,w,h),color=colors[variable];
      svg.replaceChildren();
      function element(tag,attrs,value){const el=document.createElementNS('http://www.w3.org/2000/svg',tag);Object.entries(attrs).forEach(([k,v])=>el.setAttribute(k,String(v)));if(value!=null)el.textContent=value;svg.appendChild(el);return el;}
      const path=points=>points.map((p,i)=>(i?'L ':'M ')+p.x.toFixed(1)+','+p.y.toFixed(1)).join(' ');
      [model.low,(model.high+model.low)/2,model.high].forEach(value=>{
        const y=model.padTop+(model.high-value)/(model.high-model.low)*(model.bottom-model.padTop);
        element('line',{x1:model.padX,y1:y,x2:w-model.padX,y2:y,stroke:'#e6edf2'});
        element('text',{x:model.padX-8,y:y+4,'text-anchor':'end',fill:'#617687','font-size':10},Math.round(value));
      });
      model.segments.forEach(segment=>{
        if(segment.length<2)return;
        element('path',{class:'curve-fill',d:path(segment)+' L '+segment[segment.length-1].x+','+model.bottom+' L '+segment[0].x+','+model.bottom+' Z',fill:color,opacity:.055});
        element('path',{class:'curve-line',d:path(segment),stroke:color,fill:'none'});
      });
      const stride=Math.max(1,Math.ceil(model.points.length/Math.max(2,Math.floor(w/75))));
      model.points.forEach((point,index)=>{
        const label=index%stride===0||index===model.points.length-1;
        if(label)element('text',{class:'curve-label',x:point.x,y:h-8},point.hr.hour);
        if(point.val==null)return;
        const dot=element('circle',{class:'curve-dot',cx:point.x,cy:point.y,r:w<500?3.5:4.5,fill:color,tabindex:0,role:'img','aria-label':point.hr.hour+' WIB: '+window.LangitPublicData.format(point.val,model.unit)});
        dot.addEventListener('mouseenter',()=>show(point,model.unit));dot.addEventListener('focus',()=>show(point,model.unit));dot.addEventListener('mouseleave',hide);dot.addEventListener('blur',hide);
        if(label)element('text',{class:'curve-value',x:point.x,y:Math.max(15,point.y-12)},window.LangitPublicData.format(point.val,model.unit));
      });
      const status=container.querySelector('.chart-status');if(status)status.textContent=model.points.some(point=>point.val!=null)?'':'Data parameter ini belum tersedia.';
      chips.forEach(chip=>chip.setAttribute('aria-pressed',String(chip.dataset.var===variable)));
    }
    chips.forEach(chip=>chip.addEventListener('click',()=>{
      chips.forEach(c=>c.classList.remove('active'));chip.classList.add('active');variable=chip.dataset.var;draw();setSharedLayer(variable);
    }));
    draw();document.addEventListener('langit:daychange',draw);
    document.addEventListener('langit:layerchange',event=>{
      if(!colors[event.layer]||variable===event.layer)return;
      variable=event.layer;chips.forEach(chip=>chip.classList[chip.dataset.var===variable?'add':'remove']('active'));draw();
    });
    let resizeTimer;window.addEventListener('resize',()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(draw,100);},{passive:true});
  });

  document.querySelectorAll('.day-panel').forEach(panel=>{
    const sheet=panel.querySelector('.mobile-bottom-sheet'),trigger=panel.querySelector('.hourly-toggle');
    if(!sheet||!trigger)return;
    trigger.addEventListener('click',event=>{if(window.innerWidth<768&&typeof sheet.showModal==='function'){event.preventDefault();sheet.showModal();}});
    sheet.querySelector('.sheet-close')?.addEventListener('click',()=>sheet.close());
    sheet.addEventListener('click',event=>{if(event.target===sheet){const r=sheet.getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)sheet.close();}});
  });
  // Products with one forecast day also retain a native mobile detail dialog.
  if(!document.querySelector('.day-panel'))document.querySelectorAll('.hourly-section').forEach(details=>{
    const section=details.parentElement,sheet=section.querySelector('.mobile-bottom-sheet'),trigger=details.querySelector('.hourly-toggle');
    if(!sheet||!trigger)return;
    trigger.addEventListener('click',event=>{if(innerWidth<768&&typeof sheet.showModal==='function'){event.preventDefault();sheet.showModal();}});
    sheet.querySelector('.sheet-close')?.addEventListener('click',()=>sheet.close());
  });
  document.querySelectorAll('.layer-btn').forEach(button=>button.addEventListener('click',()=>{
    setSharedLayer(button.dataset.layer);
  }));
  document.querySelectorAll('[data-copy-forecast]').forEach(button=>button.addEventListener('click',async()=>{
    const group=button.closest('.glass'),field=group?.querySelector('.share-text'),status=group?.querySelector('.copy-status');
    if(!field)return;
    try{await navigator.clipboard.writeText(field.value);if(status)status.textContent='Ringkasan tersalin.';}
    catch{field.focus();field.select();if(status)status.textContent='Teks sudah dipilih. Salin melalui menu perangkat.';}
  }));
})();
