/* Execute generated scripts against a small DOM fixture. This is not a browser. */
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const data = require('../site_tools/public_data.js');
const input = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));

class Element {
  constructor(tag='div') {
    this.tag=tag; this.children=[]; this.dataset={}; this.attributes={}; this.events={};
    this.style={}; this.textContent=''; this.innerHTML=''; this.value=''; this.hidden=true;
    const classes=new Set();
    this.classList={add:(...names)=>names.forEach(n=>classes.add(n)),
      remove:(...names)=>names.forEach(n=>classes.delete(n)), contains:n=>classes.has(n)};
  }
  setAttribute(key,value) {
    this.attributes[key]=String(value);
    if(key.startsWith('data-')) this.dataset[key.slice(5)]=String(value);
  }
  getAttribute(key) { return this.attributes[key] ?? null; }
  appendChild(element) { this.children.push(element); return element; }
  append(...elements) { elements.forEach(el=>this.appendChild(el)); }
  replaceChildren(...elements) { this.children=[...elements]; }
  addEventListener(type,fn) { this.events[type]=fn; }
  click() { if(this.onclick) this.onclick(); if(this.events.click) this.events.click({target:this}); }
  closest(selector) { return selector==='.timeline-container' && this.className==='time-pill' ? this : null; }
  scrollIntoView() {}
}
function environment(document) {
  const listeners={};
  const sandbox={document,LangitPublicData:data,Intl,Date,Map,Set,Math,Number,JSON,
    console:{error(){},warn(){}},setInterval(){return 1;},clearInterval(){},
    setTimeout(fn){return 1;},clearTimeout(){},Event:class {constructor(type){this.type=type;}}};
  sandbox.window=sandbox; sandbox.self=sandbox; sandbox.top=sandbox; sandbox.parent=sandbox;
  sandbox.location={origin:'https://langit.invalid'};
  sandbox.addEventListener=(type,fn)=>listeners[type]=fn;
  sandbox.matchMedia=()=>({matches:true});
  return {sandbox,listeners};
}

// CDN failure: every hour and textual value must remain available.
const elements={};
const el=id=>elements[id] ||= new Element();
const parameterButtons=['risiko','temp','rain','wind','humidity','cloud'].map(layer=>{
  const button=new Element('button');button.dataset.layer=layer;return button;
});
const mapEvents={};
const mapDocument={
  getElementById:el,createElement:tag=>new Element(tag),
  querySelectorAll(selector) {
    if(selector==='.time-pill') return el('time-scrubber').children;
    if(selector==='.param-tab') return parameterButtons;
    return [];
  },
  addEventListener(type,fn){mapEvents[type]=fn;}
};
const mapEnv=environment(mapDocument);
const scripts=[...input.map.matchAll(/<script>([\s\S]*?)<\/script>/g)];
vm.runInNewContext(scripts.at(-1)[1],mapEnv.sandbox);
const pills=el('time-scrubber').children;
assert.equal(pills.length,2,'all locations contribute to timeline');
assert.equal(el('map-fallback').hidden,false,'missing Leaflet exposes fallback');
assert.equal(el('val-temp').textContent,'0°C','real zero survives');
el('map-location').value='other';
el('map-location').events.change();
assert.equal(el('val-temp').textContent,'—','absent location/hour remains missing');
pills[1].click();
assert.equal(el('val-temp').textContent,'29°C');
mapEnv.sandbox.switchLayer('wind');
assert.ok(el('map-legend').innerHTML.includes('Kecepatan Angin'));
mapEnv.sandbox.togglePlay();
assert.equal(el('play-btn').getAttribute('aria-pressed'),'true');
mapEnv.sandbox.togglePlay();
assert.equal(el('play-btn').getAttribute('aria-pressed'),'false');
const legend=el('map-legend').innerHTML;
mapEnv.listeners.message({origin:'https://untrusted.invalid',source:mapEnv.sandbox,data:{type:'switchLayer',layer:'temp'}});
assert.equal(el('map-legend').innerHTML,legend,'cross-origin message is rejected');

// Render the real chart script, preserving zero and breaking a null segment.
const svg=new Element('svg');svg.clientWidth=320;svg.clientHeight=200;
const holder=new Element();holder.dataset.points=JSON.stringify([
  {hour:'06:00',rain_probability:0,temp_c:20,humidity_pct:80,wind_kmh:0},
  {hour:'07:00',rain_probability:null,temp_c:null,humidity_pct:null,wind_kmh:null},
  {hour:'08:00',rain_probability:100,temp_c:25,humidity_pct:90,wind_kmh:10}
]);
const tooltip=new Element();const tooltipParts={'.chart-tooltip-time':new Element(),'.chart-tooltip-value':new Element()};
tooltip.querySelector=selector=>tooltipParts[selector];
const chips=['rain','temp','humidity','wind'].map(variable=>{
  const chip=new Element('button');chip.dataset.var=variable;return chip;
});
const status=new Element();
const wrapper=new Element();
wrapper.querySelector=selector=>({'.chart-svg-container':holder,'.rain-curve':svg,'.chart-tooltip':tooltip,'.chart-status':status}[selector] || null);
wrapper.querySelectorAll=selector=>selector==='.chart-chip'?chips:[];
const chartEvents={};
const chartDocument={
  querySelector(){return null;},getElementById(){return null;},
  querySelectorAll(selector){return selector==='.chart-wrapper'?[wrapper]:[];},
  createElementNS(ns,tag){return new Element(tag);},
  addEventListener(type,fn){chartEvents[type]=fn;},
  dispatchEvent(event){chartEvents[event.type]?.(event);}
};
const chartEnv=environment(chartDocument);
vm.runInNewContext(input.shared,chartEnv.sandbox);
assert.equal(svg.getAttribute('viewBox'),'0 0 320 200');
assert.equal(svg.children.filter(node=>node.tag==='circle').length,2);
assert.equal(svg.children.filter(node=>node.tag==='path').length,0,'no curve bridges missing hour');
const firstDot=svg.children.find(node=>node.tag==='circle');
firstDot.events.focus({});
assert.ok(tooltipParts['.chart-tooltip-value'].textContent.startsWith('0%'));
svg.clientWidth=1440;
chartDocument.dispatchEvent({type:'langit:daychange'});
assert.equal(svg.getAttribute('viewBox'),'0 0 1440 200');
chips[3].click();
assert.equal(chips[3].getAttribute('aria-pressed'),'true');
assert.equal(chips[0].getAttribute('aria-pressed'),'false');
console.log('Generated map fallback, origin checks, timeline, chart gaps, rescaling and keyboard labels: PASS (DOM fixture, not browser)');
