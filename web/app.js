const $ = id => document.getElementById(id);
const settingIds = [
  'spacing','path_tolerance','rpm_x','rpm_y','full_steps_x','full_steps_y','microsteps_x','microsteps_y',
  'white_cutoff','threshold_red','threshold_blue',
  'threshold_green','threshold_black','mono_pen','mono_threshold',
  'bed_width','bed_height',
  'steps_x','steps_y','servo_up','servo_red','servo_blue','servo_green','servo_black',
  'pen_down_ms','pen_up_ms',
  'ramp_start_rpm','ramp_ms','corner_percent',
  ...['red','blue','green','black'].flatMap(color => [`offset_${color}_x`,`offset_${color}_y`]),
];
const penColors = {RED:'#dd4243',BLUE:'#63959e',GREEN:'#2d9669',BLACK:'#232a35'};
const ALPHA_COLORS = ['RED','BLUE','GREEN','BLACK'];
const MAX_CELLS = 2000000;
const MAX_LAYER_MM = 5000;
const MAX_FEED = 24000;
const MICROSTEP_CHOICES = [2,4,5,8,10,16,20,25,32,40,50,64,100,125,128];
// SW5–8 states from this machine's photographed DM556 table, for 200-step motors.
const DM556_SWITCHES = {
  2:['OFF','ON','ON','ON'],4:['ON','OFF','ON','ON'],8:['OFF','OFF','ON','ON'],
  16:['ON','ON','OFF','ON'],32:['OFF','ON','OFF','ON'],64:['ON','OFF','OFF','ON'],
  128:['OFF','OFF','OFF','ON'],5:['ON','ON','ON','OFF'],10:['OFF','ON','ON','OFF'],
  20:['ON','OFF','ON','OFF'],25:['OFF','OFF','ON','OFF'],40:['ON','ON','OFF','OFF'],
  50:['OFF','ON','OFF','OFF'],100:['ON','OFF','OFF','OFF'],125:['OFF','OFF','OFF','OFF'],
};
const previousMicrosteps = {x:32,y:32};
let api = null;
let paletteMode = 'four';
let orientation = 'portrait';
let previousBedHeight = 300;
let layers = [];
let selectedLayerId = null;
let plotPreview = null;
let previewMode = 'edit';
let generated = false;
let plotReady = false;
let plotWarning = '';
let dirty = false;
let busy = false;
let latestState = {connected:false,phase:'idle',progress:0,total:0};
let fanMode = 'AUTO';
let toastTimer;
let processingTimer;

function machineSize(){return {width:Number($('bed_width').value)||300,height:Number($('bed_height').value)||300};}
function pageSize(){const bed=machineSize();return orientation==='landscape'?{width:bed.height,height:bed.width}:bed;}
function clamp(value,min,max){return Math.max(min,Math.min(max,value));}
function settings(){
  const page=pageSize();
  return {...Object.fromEntries(settingIds.map(id=>[id,$(id).value])),
    palette_mode:paletteMode,orientation,canvas_mode:'machine',origin_x:0,origin_y:0,
    draw_width:page.width,draw_height:page.height};
}
function persist(){try{localStorage.setItem('citadela.settings',JSON.stringify(settings()));}catch(e){}}
function restore(){
  try{
    const old=localStorage.getItem('citadela.settings')||localStorage.getItem('chromatic.settings')||'{}';
    const values=JSON.parse(old);
    for(const id of settingIds)if(values[id]!==undefined)$(id).value=values[id];
    paletteMode=values.palette_mode==='mono'?'mono':'four';
    orientation=values.orientation==='landscape'?'landscape':'portrait';
    fanMode=localStorage.getItem('citadela.fanMode')==='MANUAL'?'MANUAL':'AUTO';
  }catch(e){}
  for(const axis of ['x','y'])previousMicrosteps[axis]=Number($('microsteps_'+axis).value);
  previousBedHeight=machineSize().height;
  updatePaletteControls();updateOrientationControls();updateResolutionInfo();updateSpeedInfo();
}
function toast(message,error=false){
  const element=$('toast');element.textContent=message;element.classList.toggle('error',error);
  element.classList.add('show');clearTimeout(toastTimer);
  toastTimer=setTimeout(()=>element.classList.remove('show'),4200);
}
function invalidate(){
  dirty=true;
  if(previewMode==='plot')showPreview('edit');
  if(generated){$('jobStatus').textContent='SETTINGS CHANGED';$('jobStatus').className='status-pill';
    $('progressText').textContent='Regenerate to use the updated page.';}
  renderControls();
  renderMachineCursor();
}
function updatePaletteControls(){
  $('modeFour').classList.toggle('selected',paletteMode==='four');
  $('modeMono').classList.toggle('selected',paletteMode==='mono');
  $('fourPaletteControls').hidden=paletteMode!=='four';
  $('monoPaletteControls').hidden=paletteMode!=='mono';
  for(const color of ['red','blue','green','black'])$(color+'ThresholdOutput').textContent=$('threshold_'+color).value+'%';
  $('cutoffOutput').textContent=$('white_cutoff').value;
  $('monoThresholdOutput').textContent=$('mono_threshold').value;
  $('monoSwatch').style.background=penColors[$('mono_pen').value]||penColors.BLACK;
  document.querySelectorAll('[data-mono-threshold]').forEach(button=>{
    const selected=Number(button.dataset.monoThreshold)===Number($('mono_threshold').value);
    button.classList.toggle('selected',selected);
    button.setAttribute('aria-pressed',String(selected));
  });
}
function applyMonoPreset(threshold){
  if(![120,225,170].includes(Number(threshold)))return;
  $('mono_threshold').value=threshold;paletteMode='mono';
  updatePaletteControls();persist();invalidate();schedulePreviews(true);
}
function updateOrientationControls(){
  $('portraitButton').classList.toggle('selected',orientation==='portrait');
  $('landscapeButton').classList.toggle('selected',orientation==='landscape');
  const page=pageSize();
  $('paperDimensions').textContent=`${page.width} × ${page.height} mm`;
  $('imageName').textContent=`Machine canvas · ${page.width} × ${page.height} mm`;
  $('imageMeta').textContent=layers.length
    ?`${layers.length} image${layers.length===1?'':'s'} · outside areas are cropped to machine travel`
    :'Full machine travel · add, drop, or paste images';
  const bed=machineSize();
  $('canvasTravel').textContent=`X ${bed.width} × Y ${bed.height} mm · set in Calibration`;
}
function updateResolutionInfo(){
  const spacing=Number($('spacing').value),page=pageSize();
  if(!Number.isFinite(spacing)||spacing<=0){$('resolutionInfo').textContent='Enter a valid line spacing.';return;}
  const cols=Math.round(page.width/spacing),rows=Math.round(page.height/spacing);
  $('resolutionInfo').textContent=`Approx. ${cols.toLocaleString()} × ${rows.toLocaleString()} plot cells${cols*rows>MAX_CELLS?' · too many for this job':''}`;
  document.querySelectorAll('[data-spacing]').forEach(button=>button.classList.toggle('selected',Math.abs(Number(button.dataset.spacing)-spacing)<0.001));
}
function updateDriverGuides(){
  for(const axis of ['x','y']){
    const microsteps=Number($('microsteps_'+axis).value);
    const switches=DM556_SWITCHES[microsteps],steps=Number($('steps_'+axis).value);
    const guide=$('driverGuide_'+axis);
    if(!switches){guide.textContent=`${axis.toUpperCase()}: select the pulse setting used on the physical driver.`;continue;}
    const states=switches.map((value,index)=>`SW${index+5} ${value}`).join(' · ');
    const resolution=Number.isFinite(steps)&&steps>0?` · ${(1/steps).toFixed(5)} mm/pulse`:'';
    guide.textContent=`${axis.toUpperCase()}: ${states} · ${(200*microsteps).toLocaleString()} pulses/rev for a 200-step motor${resolution}`;
  }
}
function updateSpeedInfo(){
  updateDriverGuides();
  const info=[];
  for(const axis of ['x','y']){
    const rpm=Number($('rpm_'+axis).value);
    const steps=Number($('full_steps_'+axis).value);
    const microsteps=Number($('microsteps_'+axis).value);
    if(!Number.isFinite(rpm)||rpm<1||rpm>600||!Number.isInteger(steps)||steps<1||steps>2000||
       !MICROSTEP_CHOICES.includes(microsteps)){
      $('speedInfo').textContent='Enter valid RPM, full steps/rev, and microsteps for both axes.';
      return;
    }
    const requested=rpm*steps*microsteps/60;
    const actual=Math.min(requested,MAX_FEED);
    const actualRpm=actual*60/(steps*microsteps);
    info.push(`${axis.toUpperCase()} up to ${actualRpm.toFixed(1)} RPM · ${Math.round(actual).toLocaleString()} steps/s${requested>MAX_FEED?' (capped)':''}`);
  }
  $('speedInfo').textContent=info.join('  |  ')+` · Motion firmware v5 target ceiling: ${MAX_FEED.toLocaleString()} steps/s. Short moves may not reach the RPM limit.`;
}
function rescaleMicrostepCalibration(axis){
  const next=Number($('microsteps_'+axis).value),previous=previousMicrosteps[axis];
  if(!MICROSTEP_CHOICES.includes(next))return;
  const steps=Number($('steps_'+axis).value);
  if(Number.isFinite(previous)&&previous>0&&Number.isFinite(steps)&&steps>0&&next!==previous){
    $('steps_'+axis).value=Number((steps*next/previous).toFixed(4));
  }
  previousMicrosteps[axis]=next;
}
function resolutionValid(){
  const spacing=Number($('spacing').value),page=pageSize();
  return spacing>=0.2&&spacing<=5&&Math.round(page.width/spacing)*Math.round(page.height/spacing)<=MAX_CELLS;
}
function paperPoint(position,offsetX=0,offsetY=0){
  const stepsX=Number($('steps_x').value),stepsY=Number($('steps_y').value);
  const x=Number(position?.x),y=Number(position?.y);
  if(![stepsX,stepsY,x,y,offsetX,offsetY].every(Number.isFinite)||stepsX<=0||stepsY<=0)return null;
  return {x:x/stepsX+offsetX,y:y/stepsY+offsetY};
}
function viewPoint(point){
  return point&&orientation==='landscape'?{x:machineSize().height-point.y,y:point.x}:point;
}
function renderMachineCursor(){
  const overlay=$('machineOverlay'),carriageCursor=$('carriageCursor'),penCursor=$('penCursor');
  if(!latestState.connected||!latestState.position){
    overlay.hidden=true;
    $('carriagePosition').textContent='Connect to show the live carriage cursor.';
    $('penPosition').textContent='Marker: unavailable';
    return;
  }
  const carriage=paperPoint(latestState.position),page=pageSize();
  if(!carriage){
    overlay.hidden=true;
    $('carriagePosition').textContent='Check steps/mm to show the cursor.';
    $('penPosition').textContent='Marker: unavailable';
    return;
  }
  const bed=machineSize();
  const inside=point=>point&&point.x>=0&&point.y>=0&&point.x<=bed.width&&point.y<=bed.height;
  const pen=String(latestState.pen||'UP').toUpperCase();
  const hasPen=ALPHA_COLORS.includes(pen);
  const tip=hasPen?paperPoint(latestState.position,
    Number($('offset_'+pen.toLowerCase()+'_x').value),
    Number($('offset_'+pen.toLowerCase()+'_y').value)):null;
  $('carriagePosition').textContent=`Estimated carriage · X ${carriage.x.toFixed(1)} · Y ${carriage.y.toFixed(1)} mm${inside(carriage)?'':' · outside travel'}`;
  $('penPosition').textContent=tip
    ?`${pen[0]+pen.slice(1).toLowerCase()} marker · X ${tip.x.toFixed(1)} · Y ${tip.y.toFixed(1)} mm${inside(tip)?'':' · outside travel'}`
    :hasPen?'Check this pen’s tip offset to show its marker.':'Marker: pen up';
  const stage=$('pageStage'),target=previewMode==='plot'?$('plotImage'):$('paperSheet');
  const targetRect=target.getBoundingClientRect(),stageRect=stage.getBoundingClientRect();
  if(!targetRect.width||!targetRect.height){overlay.hidden=true;return;}
  overlay.style.left=(targetRect.left-stageRect.left-stage.clientLeft)+'px';
  overlay.style.top=(targetRect.top-stageRect.top-stage.clientTop)+'px';
  overlay.style.width=targetRect.width+'px';
  overlay.style.height=targetRect.height+'px';
  carriageCursor.hidden=!inside(carriage);
  if(!carriageCursor.hidden){
    const point=viewPoint(carriage);
    carriageCursor.style.left=100*point.x/page.width+'%';
    carriageCursor.style.top=100*point.y/page.height+'%';
  }
  penCursor.hidden=!inside(tip);
  if(!penCursor.hidden){
    const point=viewPoint(tip);
    penCursor.style.left=100*point.x/page.width+'%';
    penCursor.style.top=100*point.y/page.height+'%';
    penCursor.style.setProperty('--pen-color',penColors[pen]);
  }
  overlay.hidden=carriageCursor.hidden&&penCursor.hidden;
}
function showPreview(mode){
  if(mode==='plot'&&(!plotPreview||dirty)){toast('Generate a fresh plot preview first.');return;}
  previewMode=mode;
  $('editTab').classList.toggle('selected',mode==='edit');
  $('plotTab').classList.toggle('selected',mode==='plot');
  $('paperSheet').hidden=mode==='plot';
  $('plotImage').hidden=mode!=='plot';
  if(mode==='plot')$('plotImage').src=plotPreview;
  requestAnimationFrame(renderMachineCursor);
}
function setView(name){
  document.querySelectorAll('.nav').forEach(el=>el.classList.toggle('active',el.dataset.view===name));
  $('studioView').classList.toggle('active',name==='studio');
  $('calibrationView').classList.toggle('active',name==='calibration');
  $('pageTitle').textContent=name.toUpperCase();
  $('mainTitle').textContent=name==='studio'?'Make images move.':'Tune your machine.';
  $('subtitle').textContent=name==='studio'?'Compose artwork across the full machine travel and turn it into connected paths.':'Set your X/Y geometry, test the pens, and establish zero.';
  if(name==='studio')requestAnimationFrame(renderPage);
}
function renderControls(){
  const plotting=latestState.phase==='plotting';
  $('connectButton').textContent=latestState.connected?'Disconnect':'Connect machine';
  $('connectionDot').classList.toggle('online',!!latestState.connected);
  $('connectionLabel').textContent=latestState.connected?'Machine connected':'Disconnected';
  $('connectionHint').textContent=latestState.connected?latestState.port:'Connect an Arduino Uno to begin.';
  $('startButton').disabled=busy||!latestState.connected||!generated||!plotReady||dirty||plotting;
  $('stopButton').disabled=!latestState.connected||!plotting;
  $('saveButton').disabled=!generated||!plotReady||dirty;
  $('generateButton').disabled=busy||layers.length===0||plotting||!resolutionValid();
  $('portSelect').disabled=busy||!!latestState.connected;
  $('refreshPorts').disabled=busy||!!latestState.connected;
  ['addImagesFooter','addImagesPanel'].forEach(id=>$(id).disabled=busy||plotting);
  document.querySelectorAll('[data-action]').forEach(el=>el.disabled=busy||!latestState.connected||plotting);
  $('calibrationStop').disabled=!latestState.connected;
  document.querySelectorAll('#layerControls input,#layerControls button').forEach(el=>el.disabled=plotting||busy);
  renderFan();
}
function renderFan(){
  const connected=!!latestState.connected;
  const mode=connected?(latestState.fan_mode||fanMode):fanMode;
  const on=connected&&!!latestState.fan_on;
  const pending=!!latestState.fan_pending;
  const automatic=mode==='AUTO';
  $('fanAuto').classList.toggle('selected',automatic);
  $('fanManual').classList.toggle('selected',!automatic);
  $('fanAuto').setAttribute('aria-pressed',String(automatic));
  $('fanManual').setAttribute('aria-pressed',String(!automatic));
  $('fanAuto').disabled=busy||pending;
  $('fanManual').disabled=busy||pending;
  $('fanPower').disabled=busy||pending||!connected;
  $('fanPower').setAttribute('aria-checked',String(on));
  $('fanManualControls').hidden=automatic;
  $('fanCycle').hidden=!automatic;
  $('fanStatus').textContent=!connected?'DISCONNECTED':pending?'UPDATING':on?'FAN ON':'FAN OFF';
  $('fanStatus').classList.toggle('fan-running',on);
  $('fanDescription').textContent=automatic
    ?'Repeats 2,000 instructions off, then 3,000 on. Each plot starts a fresh cycle.'
    :'Turn cooling on or off here, including while a plot is running.';
  const count=Math.max(Number(latestState.fan_count)||0,
    latestState.fan_active?Number(latestState.progress)||0:0);
  const phase=count%5000;
  $('fanCount').textContent=count.toLocaleString()+' instructions';
  $('fanCycleProgress').style.width=(phase/5000*100)+'%';
  $('fanCyclePhase').textContent=!connected?'Connect the Uno':!latestState.fan_active?'Waiting for a plot'
    :phase<2000?`${(2000-phase).toLocaleString()} until fan on`
    :`${(5000-phase).toLocaleString()} until fan off`;
}
async function changeFan(mode,on=false){
  await run(async()=>{
    if(latestState.connected)renderState(await call('set_fan',mode,on));
    fanMode=mode;
    try{localStorage.setItem('citadela.fanMode',mode);}catch(e){}
    renderFan();
  });
}
function renderState(state){
  latestState=state;renderControls();
  $('jobStatus').textContent=dirty&&generated?'SETTINGS CHANGED':(state.phase||'idle').toUpperCase();
  $('jobStatus').className='status-pill '+(dirty?'':state.phase||'');
  const percent=state.total?Math.round(100*state.progress/state.total):0;
  $('progressBar').style.width=`${percent}%`;$('progressPercent').textContent=percent+'%';
  if(!dirty||!generated)$('progressText').textContent=generated&&!plotReady
    ?plotWarning:(state.message||'Waiting for artwork.');
  if(state.position)$('positionReadout').textContent=`X ${Number(state.position.x).toLocaleString()} steps · Y ${Number(state.position.y).toLocaleString()} steps`;
  if(state.log)$('serialLog').textContent=state.log.length?state.log.join('\n'):'No serial messages yet.';
  renderMachineCursor();
}
async function call(method,...args){
  if(!api)throw new Error('Desktop bridge is still starting.');
  try{return await api[method](...args);}catch(err){throw new Error(err?.message||String(err));}
}
async function run(action){
  if(busy)return;busy=true;renderControls();
  try{await action();}catch(err){toast(err.message,true);}finally{busy=false;renderControls();}
}
async function refreshPorts(){
  const ports=await call('ports'),current=$('portSelect').value;
  $('portSelect').innerHTML='<option value="">Select serial port</option>';
  ports.forEach(port=>{const option=document.createElement('option');option.value=port;option.textContent=port;$('portSelect').appendChild(option);});
  if(ports.includes(current))$('portSelect').value=current;
  else {
    const usb=ports.filter(port=>/usb|modem|acm/i.test(port));
    if(usb.length===1)$('portSelect').value=usb[0];
    else if(ports.length===1)$('portSelect').value=ports[0];
  }
}

function renderPage(){
  const stage=$('pageStage'),sheet=$('paperSheet'),page=pageSize();
  if(!stage.clientWidth||!stage.clientHeight)return;
  const scale=Math.min((stage.clientWidth-48)/page.width,(stage.clientHeight-48)/page.height);
  sheet.style.width=Math.round(page.width*scale)+'px';
  sheet.style.height=Math.round(page.height*scale)+'px';
  renderLayers();
  renderMachineCursor();
}
function frameStyle(frame,layer){
  const page=pageSize();
  frame.style.left=100*layer.x_mm/page.width+'%';
  frame.style.top=100*layer.y_mm/page.height+'%';
  frame.style.width=100*layer.width_mm/page.width+'%';
  frame.style.height=100*layer.height_mm/page.height+'%';
}
function renderLayers(){
  const surface=$('layerSurface');surface.replaceChildren();
  layers.forEach((layer,index)=>{
    const frame=document.createElement('div');frame.className='layer-frame'+(layer.id===selectedLayerId?' selected':'');
    frame.dataset.layerId=layer.id;frameStyle(frame,layer);
    const image=document.createElement('img');image.src=layer.filteredPreview||layer.preview;image.alt=layer.name;
    const handle=document.createElement('span');handle.className='resize-handle';handle.title='Drag to resize';
    frame.append(image,handle);frame.addEventListener('pointerdown',event=>startPointer(event,layer,frame));
    surface.appendChild(frame);
  });
  updateOrientationControls();
}
function renderLayerList(){
  const list=$('layerList');list.replaceChildren();
  if(!layers.length){const empty=document.createElement('div');empty.className='layer-empty';empty.textContent='No images yet. Add one or select several at once.';list.appendChild(empty);}
  layers.forEach((layer,index)=>{
    const button=document.createElement('button');button.className='layer-row'+(layer.id===selectedLayerId?' selected':'');
    const thumb=document.createElement('img');thumb.src=layer.preview;thumb.alt='';
    const details=document.createElement('span');const name=document.createElement('strong');name.textContent=layer.name;
    const info=document.createElement('small');info.textContent=`${layer.width}px × ${layer.height}px`;
    details.append(name,info);const order=document.createElement('b');order.className='layer-index';order.textContent=String(index+1);
    button.append(thumb,details,order);button.addEventListener('click',()=>selectLayer(layer.id));list.appendChild(button);
  });
  $('layerControls').hidden=!selectedLayer();
  syncLayerControls();
  updateOrientationControls();
}
function selectedLayer(){return layers.find(layer=>layer.id===selectedLayerId)||null;}
function selectLayer(id){
  selectedLayerId=id;
  document.querySelectorAll('.layer-frame').forEach(frame=>frame.classList.toggle('selected',frame.dataset.layerId===id));
  renderLayerList();
}
function syncLayerControls(){
  const layer=selectedLayer();if(!layer)return;
  $('selectedLayerName').textContent=layer.name;
  $('selectedLayerSize').textContent=`${layer.width_mm.toFixed(1)} × ${layer.height_mm.toFixed(1)} mm`;
  $('layerX').value=layer.x_mm.toFixed(1);$('layerY').value=layer.y_mm.toFixed(1);
  const scale=Math.round(100*layer.width_mm/layer.baseWidth);
  $('layerScale').value=clamp(scale,1,1000);$('layerScaleOutput').textContent=scale+'%';
  $('layerOpacity').value=layer.alpha;$('layerOpacityOutput').textContent=layer.alpha+'%';
  for(const color of ALPHA_COLORS){
    const title=color[0]+color.slice(1).toLowerCase();
    $('layer'+title).value=layer.color_alpha[color];
    $('layer'+title+'Output').textContent=layer.color_alpha[color]+'%';
  }
}
function startPointer(event,layer,frame){
  if(latestState.phase==='plotting'||previewMode!=='edit')return;
  event.preventDefault();event.stopPropagation();selectLayer(layer.id);
  const resizing=event.target.classList.contains('resize-handle');
  const start={x:layer.x_mm,y:layer.y_mm,width:layer.width_mm,height:layer.height_mm,
    clientX:event.clientX,clientY:event.clientY};
  const page=pageSize(),sheet=$('paperSheet');
  frame.setPointerCapture(event.pointerId);
  const onMove=move=>{
    const dx=(move.clientX-start.clientX)*page.width/sheet.clientWidth;
    const dy=(move.clientY-start.clientY)*page.height/sheet.clientHeight;
    if(resizing){
      const requested=start.width+(Math.abs(dx)>Math.abs(dy*layer.aspect)?dx:dy*layer.aspect);
      const maxWidth=Math.min(MAX_LAYER_MM,MAX_LAYER_MM*layer.aspect);
      layer.width_mm=clamp(requested,0.1,maxWidth);
      layer.height_mm=layer.width_mm/layer.aspect;
    }else{
      layer.x_mm=clamp(start.x+dx,-MAX_LAYER_MM,MAX_LAYER_MM);
      layer.y_mm=clamp(start.y+dy,-MAX_LAYER_MM,MAX_LAYER_MM);
    }
    frameStyle(frame,layer);syncLayerControls();invalidate();
  };
  const onEnd=()=>{
    frame.removeEventListener('pointermove',onMove);
    frame.removeEventListener('pointerup',onEnd);
    frame.removeEventListener('pointercancel',onEnd);
    renderLayers();renderLayerList();
  };
  frame.addEventListener('pointermove',onMove);
  frame.addEventListener('pointerup',onEnd);
  frame.addEventListener('pointercancel',onEnd);
}

function classifyPixel(r,g,b){
  if((r+g+b)/3>=Number($('white_cutoff').value)&&Math.max(r,g,b)-Math.min(r,g,b)<42)return null;
  const max=Math.max(r,g,b),min=Math.min(r,g,b),delta=max-min;
  const saturation=max?delta/max:0,value=max/255;
  let hue=0;
  if(delta){
    if(max===r)hue=((g-b)/delta)%6;
    else if(max===g)hue=(b-r)/delta+2;
    else hue=(r-g)/delta+4;
    hue=(hue*60+360)%360;
  }
  let best=null,bestScore=0;
  for(const [color,center] of [['RED',0],['GREEN',120],['BLUE',205]]){
    let distance=Math.abs(hue-center);distance=Math.min(distance,360-distance);
    const score=saturation*Math.max(0,1-distance/100)*Math.min(1,value/0.15);
    if(score>0&&score*100>=Number($('threshold_'+color.toLowerCase()).value)&&score>bestScore){best=color;bestScore=score;}
  }
  if(best)return best;
  const darkness=100*(1-(0.2126*r+0.7152*g+0.0722*b)/255);
  return darkness>=Number($('threshold_black').value)?'BLACK':null;
}
function processLayerPreview(layer){
  if(!layer.sourceImage)return;
  const image=layer.sourceImage,canvas=document.createElement('canvas');
  canvas.width=image.naturalWidth;canvas.height=image.naturalHeight;
  const context=canvas.getContext('2d',{willReadFrequently:true});context.drawImage(image,0,0);
  const pixels=context.getImageData(0,0,canvas.width,canvas.height),data=pixels.data;
  for(let index=0;index<data.length;index+=4){
    if(!data[index+3])continue;
    const color=classifyPixel(data[index],data[index+1],data[index+2]);
    data[index+3]=color?Math.round(data[index+3]*layer.alpha*layer.color_alpha[color]/10000):0;
  }
  context.putImageData(pixels,0,0);
  const rotation=layer.rotation||0;
  if(rotation){
    const rotated=document.createElement('canvas'),quarter=rotation===90||rotation===270;
    rotated.width=quarter?canvas.height:canvas.width;
    rotated.height=quarter?canvas.width:canvas.height;
    const output=rotated.getContext('2d');
    output.translate(rotated.width/2,rotated.height/2);output.rotate(rotation*Math.PI/180);
    output.drawImage(canvas,-canvas.width/2,-canvas.height/2);
    layer.filteredPreview=rotated.toDataURL('image/png');
  }else layer.filteredPreview=canvas.toDataURL('image/png');
}
function schedulePreviews(all=false){
  clearTimeout(processingTimer);
  processingTimer=setTimeout(()=>{
    (all?layers:[selectedLayer()].filter(Boolean)).forEach(processLayerPreview);
    renderLayers();
  },100);
}
function loadImage(url){return new Promise((resolve,reject)=>{
  const image=new Image();image.onload=()=>resolve(image);image.onerror=()=>reject(new Error('Could not preview an imported image.'));image.src=url;
});}
async function addImportedItems(items){
  if(!items.length)return;
  const page=pageSize();
  for(const item of items){
    const aspect=item.width/item.height;
    const width=Math.min(page.width*0.65,page.height*0.65*aspect);
    const height=width/aspect;
    const stagger=(layers.length%5)*6;
    const layer={id:item.id,name:item.name,width:item.width,height:item.height,
      preview:item.preview,filteredPreview:null,sourceImage:await loadImage(item.preview),
      aspect,baseWidth:width,width_mm:width,height_mm:height,
      x_mm:clamp((page.width-width)/2+stagger,0,page.width-width),
      y_mm:clamp((page.height-height)/2+stagger,0,page.height-height),
      rotation:0,alpha:100,color_alpha:{RED:100,BLUE:100,GREEN:100,BLACK:100}};
    processLayerPreview(layer);layers.push(layer);selectedLayerId=layer.id;
  }
  renderPage();renderLayerList();showPreview('edit');invalidate();renderState(await call('get_state'));
  toast(`Added ${items.length} image${items.length===1?'':'s'} to the machine canvas.`);
}
async function pickImages(){await run(async()=>addImportedItems(await call('pick_images')));}
function fileDataUrl(file){return new Promise((resolve,reject)=>{
  const reader=new FileReader();reader.onload=()=>resolve(reader.result);
  reader.onerror=()=>reject(new Error(`Could not read ${file.name||'the clipboard image'}.`));
  reader.readAsDataURL(file);
});}
async function importFiles(files){
  if(!files.length||latestState.phase==='plotting')return;
  await run(async()=>{
    const items=[];
    for(const file of files){
      const data=await fileDataUrl(file);
      items.push(await call('paste_image',data,file.name||'Pasted image.png'));
    }
    await addImportedItems(items);
  });
}
function serializableLayers(){return layers.map(layer=>({id:layer.id,x_mm:layer.x_mm,y_mm:layer.y_mm,
  width_mm:layer.width_mm,height_mm:layer.height_mm,rotation:layer.rotation||0,alpha:layer.alpha,color_alpha:layer.color_alpha}));}
function durationLabel(seconds){
  const minutes=Math.max(1,Math.ceil(seconds/60));
  return minutes>=60?`${Math.floor(minutes/60)} h ${minutes%60} min`:`${minutes} min`;
}
async function generate(){await run(async()=>{
  const result=await call('generate',settings(),serializableLayers());
  plotPreview=result.preview;generated=true;plotReady=!!result.plot_ready;
  plotWarning=result.plot_ready?'':result.message;dirty=false;
  $('strokeCount').textContent=Number(result.strokes).toLocaleString();
  $('instructionCount').textContent=Number(result.instructions).toLocaleString();
  $('sizeCount').textContent=`${result.width_mm} × ${result.height_mm}`;
  const metrics=result.metrics||{};
  $('drawDistance').textContent=((metrics.draw_mm||0)/1000).toFixed(1)+' m';
  $('travelDistance').textContent=((metrics.travel_mm||0)/1000).toFixed(1)+' m';
  $('estimatedTime').textContent=durationLabel(metrics.estimated_seconds||0);
  $('plannerInfo').textContent=`Generated in ${Number(metrics.generation_seconds||0).toFixed(1)} s · Minimum time from machine zero at the RPM limits, including servo waits. Easing, corners, and serial delays add time.`;
  for(const color of ALPHA_COLORS){const title=color[0]+color.slice(1).toLowerCase();
    $('count'+title).textContent=`${Number(result.counts[color]).toLocaleString()} strokes`;}
  showPreview('plot');renderState(await call('get_state'));
  toast(result.plot_ready?'Machine plot instructions are ready.':result.message,!result.plot_ready);
});}
function setOrientation(next){
  if(next===orientation||latestState.phase==='plotting')return;
  const old=pageSize();orientation=next;
  layers.forEach(layer=>{
    const {x_mm:x,y_mm:y,width_mm:width,height_mm:height}=layer;
    const clockwise=next==='landscape';
    layer.x_mm=clockwise?old.height-y-height:y;
    layer.y_mm=clockwise?x:old.width-x-width;
    layer.width_mm=height;layer.height_mm=width;
    layer.rotation=((layer.rotation||0)+(clockwise?90:270))%360;
    layer.baseWidth/=layer.aspect;layer.aspect=1/layer.aspect;
    processLayerPreview(layer);
  });
  updateOrientationControls();updateResolutionInfo();renderPage();renderLayerList();persist();invalidate();
}
function fitSelectedToCanvas(){
  const layer=selectedLayer();if(!layer||latestState.phase==='plotting')return;
  const page=pageSize();
  layer.width_mm=Math.min(page.width,page.height*layer.aspect);
  layer.height_mm=layer.width_mm/layer.aspect;
  layer.x_mm=(page.width-layer.width_mm)/2;layer.y_mm=(page.height-layer.height_mm)/2;
  renderLayers();renderLayerList();invalidate();
}
function setLayerScale(percent){
  const layer=selectedLayer();if(!layer)return;
  const requested=layer.baseWidth*percent/100;
  const maxWidth=Math.min(MAX_LAYER_MM,MAX_LAYER_MM*layer.aspect);
  layer.width_mm=clamp(requested,0.1,maxWidth);
  layer.height_mm=layer.width_mm/layer.aspect;
  renderLayers();syncLayerControls();invalidate();
}
function moveSelectedTo(edge){
  const index=layers.findIndex(layer=>layer.id===selectedLayerId);if(index<0)return;
  const [layer]=layers.splice(index,1);
  if(edge==='front')layers.push(layer);else layers.unshift(layer);
  renderLayers();renderLayerList();invalidate();
}
async function connect(){await run(async()=>{
  const state=latestState.connected?await call('disconnect'):await call('connect',$('portSelect').value);
  renderState(state);
  if(state.connected)renderState(await call('set_fan',fanMode,false));
  toast(state.connected?'Arduino connected.':'Arduino disconnected.');
});}
async function calibrate(action){await run(async()=>{
  const state=await call('calibrate',action,settings(),Number($('jogDistance').value));
  renderState(state);toast(action==='zero'?'Position zeroed.':`Command sent: ${action.replace('_',' ')}`);
});}
async function stopNow(){try{renderState(await call('stop'));toast('Stop sent to the Arduino.');}
  catch(err){toast(err.message,true);}}

document.querySelectorAll('.nav').forEach(el=>el.addEventListener('click',()=>setView(el.dataset.view)));
for(const id of settingIds){$(id).addEventListener('input',()=>{
  if(id==='microsteps_x'||id==='microsteps_y')rescaleMicrostepCalibration(id.slice(-1));
  updatePaletteControls();updateResolutionInfo();updateSpeedInfo();persist();invalidate();
  if(id.startsWith('threshold_')||id==='white_cutoff'||id==='mono_threshold'||id==='mono_pen')schedulePreviews(true);
  if(id==='bed_width'||id==='bed_height'){
    const height=Number($('bed_height').value);
    if(Number.isFinite(height)&&height>=20&&height<=1000){
      if(orientation==='landscape')layers.forEach(layer=>layer.x_mm+=height-previousBedHeight);
      previousBedHeight=height;
    }
    renderPage();
  }
});}
$('modeFour').addEventListener('click',()=>{paletteMode='four';updatePaletteControls();persist();invalidate();});
$('modeMono').addEventListener('click',()=>{paletteMode='mono';updatePaletteControls();persist();invalidate();});
document.querySelectorAll('[data-mono-threshold]').forEach(button=>button.addEventListener('click',()=>applyMonoPreset(button.dataset.monoThreshold)));
$('portraitButton').addEventListener('click',()=>setOrientation('portrait'));
$('landscapeButton').addEventListener('click',()=>setOrientation('landscape'));
document.querySelectorAll('[data-spacing]').forEach(button=>button.addEventListener('click',()=>{
  $('spacing').value=button.dataset.spacing;updateResolutionInfo();persist();invalidate();}));
['addImagesFooter','addImagesPanel'].forEach(id=>$(id).addEventListener('click',pickImages));
document.addEventListener('paste',event=>{
  const files=Array.from(event.clipboardData?.items||[])
    .filter(item=>item.type.startsWith('image/')).map(item=>item.getAsFile()).filter(Boolean);
  if(!files.length)return;
  event.preventDefault();importFiles(files);
});
$('pageStage').addEventListener('dragover',event=>event.preventDefault());
$('pageStage').addEventListener('drop',event=>{
  event.preventDefault();importFiles(Array.from(event.dataTransfer?.files||[]));
});
$('editTab').addEventListener('click',()=>showPreview('edit'));
$('plotTab').addEventListener('click',()=>showPreview('plot'));
$('pageStage').addEventListener('pointerdown',event=>{
  if(event.target===$('pageStage')||event.target===$('paperSheet')||event.target.classList.contains('paper-grid'))selectLayer(null);
});
$('layerScale').addEventListener('input',()=>setLayerScale(Number($('layerScale').value)));
for(const [id,property] of [['layerX','x_mm'],['layerY','y_mm']])$(id).addEventListener('change',()=>{
  const layer=selectedLayer();if(!layer)return;
  layer[property]=clamp(Number($(id).value)||0,-MAX_LAYER_MM,MAX_LAYER_MM);
  renderLayers();syncLayerControls();invalidate();
});
for(const color of ALPHA_COLORS){
  const title=color[0]+color.slice(1).toLowerCase();
  $('layer'+title).addEventListener('input',()=>{
    const layer=selectedLayer();if(!layer)return;
    layer.color_alpha[color]=Number($('layer'+title).value);
    $('layer'+title+'Output').textContent=layer.color_alpha[color]+'%';
    schedulePreviews();invalidate();
  });
}
$('layerOpacity').addEventListener('input',()=>{
  const layer=selectedLayer();if(!layer)return;layer.alpha=Number($('layerOpacity').value);
  $('layerOpacityOutput').textContent=layer.alpha+'%';schedulePreviews();invalidate();
});
$('layerBack').addEventListener('click',()=>moveSelectedTo('back'));
$('layerFront').addEventListener('click',()=>moveSelectedTo('front'));
$('layerRemove').addEventListener('click',()=>{
  const index=layers.findIndex(layer=>layer.id===selectedLayerId);if(index<0)return;
  layers.splice(index,1);selectedLayerId=layers.length?layers[Math.max(0,index-1)].id:null;
  renderPage();renderLayerList();showPreview('edit');invalidate();
});
$('layerFit').addEventListener('click',fitSelectedToCanvas);
$('generateButton').addEventListener('click',generate);
$('refreshPorts').addEventListener('click',()=>run(refreshPorts));
$('connectButton').addEventListener('click',connect);
$('startButton').addEventListener('click',()=>run(async()=>{renderState(await call('start_job'));toast('Plotting started.');}));
$('stopButton').addEventListener('click',stopNow);
$('calibrationStop').addEventListener('click',stopNow);
$('fanAuto').addEventListener('click',()=>changeFan('AUTO'));
$('fanManual').addEventListener('click',()=>changeFan('MANUAL',!!latestState.fan_on));
$('fanPower').addEventListener('click',()=>changeFan('MANUAL',!latestState.fan_on));
$('saveButton').addEventListener('click',()=>run(async()=>{const path=await call('save_job');if(path)toast('Saved instructions to '+path);}));
document.querySelectorAll('[data-action]').forEach(el=>el.addEventListener('click',()=>calibrate(el.dataset.action)));
document.addEventListener('keydown',event=>{if(event.key==='Escape'&&latestState.connected)stopNow();});
window.addEventListener('resize',renderPage);
$('plotImage').addEventListener('load',renderMachineCursor);
restore();renderPage();renderLayerList();renderControls();
window.addEventListener('pywebviewready',async()=>{
  api=window.pywebview.api;
  try{await refreshPorts();renderState(await call('get_state'));}catch(err){toast(err.message,true);}
  setInterval(async()=>{try{renderState(await call('get_state'));}catch(e){}},250);
});
