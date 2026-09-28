// Coordinate and sizing checks against the actual canvas controller, with a small DOM stub.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
function element(value = '') {
  return {value, style: {setProperty(){}}, dataset: {}, classList: {toggle(){}, contains(){return false;}},
    clientWidth: 0, clientHeight: 0, append(){}, appendChild(){}, replaceChildren(){},
    addEventListener(){}, removeEventListener(){}, setAttribute(){}};
}
const elements = new Map();
for (const match of html.matchAll(/<[^>]*\bid="([^"]+)"[^>]*>/g)) {
  const value = match[0].match(/\bvalue="([^"]*)"/);
  elements.set(match[1], element(value ? value[1] : ''));
}
const document = {getElementById(id) {
  assert(elements.has(id), `Missing HTML element ${id}`); return elements.get(id);
}, querySelectorAll(){return [];}, addEventListener(){}, createElement(){return element();}};
const context = vm.createContext({document, window: {addEventListener(){}},
  localStorage: {getItem(){return null;}, setItem(){}}, setTimeout(){}, clearTimeout(){},
  requestAnimationFrame(){}, console});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8') + `
this.canvasTests={pageSize,settings,paperPoint,viewPoint,setOrientation,fitSelectedToCanvas,updateSpeedInfo,rescaleMicrostepCalibration,
  configure(width,height,items){
    $('bed_width').value=width;$('bed_height').value=height;
    orientation='portrait';previousBedHeight=height;layers=items;selectedLayerId=items[0]?.id;
  }, currentLayer(){return layers[0];},
  motion(axis,steps,microsteps,rpm){
    $('steps_'+axis).value=steps;$('microsteps_'+axis).value=microsteps;
    $('full_steps_'+axis).value=200;$('rpm_'+axis).value=rpm;
    previousMicrosteps[axis]=Number(microsteps);
  }};`, context);
const controller = context.canvasTests;
const image = {id:'test',name:'Test',preview:'',width:100,height:60,rotation:0,
  x_mm:20,y_mm:10,width_mm:100,height_mm:60,baseWidth:100,aspect:100/60,
  alpha:100,color_alpha:{RED:100,BLUE:100,GREEN:100,BLACK:100}};
controller.configure(250,500,[image]);
assert.equal(controller.pageSize().width,250);
assert.equal(controller.settings().canvas_mode,'machine');
assert.equal(controller.settings().origin_x,0);
controller.setOrientation('landscape');
assert.equal(controller.pageSize().width,500);
assert.equal(controller.pageSize().height,250);
assert.equal(image.x_mm,430);
assert.equal(image.y_mm,20);
assert.equal(image.width_mm,60);
assert.equal(image.rotation,90);
const point=controller.viewPoint({x:20,y:10});
assert.equal(point.x,490);
assert.equal(point.y,20);
controller.setOrientation('portrait');
assert.equal(image.x_mm,20);
assert.equal(image.y_mm,10);
assert.equal(image.width_mm,100);
assert.equal(image.height_mm,60);
assert.equal(image.rotation,0);
controller.fitSelectedToCanvas();
assert.equal(image.width_mm,250);
assert.equal(image.height_mm,150);
assert.equal(image.x_mm,0);
assert.equal(image.y_mm,175);
controller.motion('x',800,32,600);
controller.motion('y',800,32,600);
controller.updateSpeedInfo();
assert(elements.get('speedInfo').textContent.includes('225.0 RPM'));
assert(elements.get('driverGuide_x').textContent.includes('SW5 OFF · SW6 ON · SW7 OFF · SW8 ON'));
assert(elements.get('driverGuide_x').textContent.includes('6,400 pulses/rev'));
assert(elements.get('driverGuide_x').textContent.includes('0.00125 mm/pulse'));
elements.get('microsteps_x').value='16';
controller.rescaleMicrostepCalibration('x');
assert.equal(Number(elements.get('steps_x').value),400);
controller.updateSpeedInfo();
assert(elements.get('speedInfo').textContent.includes('X up to 450.0 RPM'));
elements.get('microsteps_y').value='128';
controller.rescaleMicrostepCalibration('y');
assert.equal(Number(elements.get('steps_y').value),3200);
elements.get('microsteps_y').value='0';
controller.rescaleMicrostepCalibration('y');
assert.equal(Number(elements.get('steps_y').value),3200);
elements.get('microsteps_y').value='32';
controller.rescaleMicrostepCalibration('y');
assert.equal(Number(elements.get('steps_y').value),800);
// Every photographed switch row is exposed by both dropdowns, with the current setting retained.
const driverRows = [
  [2,400,'OFF ON ON ON'],[4,800,'ON OFF ON ON'],[8,1600,'OFF OFF ON ON'],
  [16,3200,'ON ON OFF ON'],[32,6400,'OFF ON OFF ON'],[64,12800,'ON OFF OFF ON'],
  [128,25600,'OFF OFF OFF ON'],[5,1000,'ON ON ON OFF'],[10,2000,'OFF ON ON OFF'],
  [20,4000,'ON OFF ON OFF'],[25,5000,'OFF OFF ON OFF'],[40,8000,'ON ON OFF OFF'],
  [50,10000,'OFF ON OFF OFF'],[100,20000,'ON OFF OFF OFF'],[125,25000,'OFF OFF OFF OFF'],
];
for(const [microsteps,pulses,states] of driverRows){
  controller.motion('x',microsteps*25,microsteps,120);
  controller.updateSpeedInfo();
  const guide=elements.get('driverGuide_x').textContent;
  assert(guide.includes(states.split(' ').map((state,index)=>`SW${index+5} ${state}`).join(' · ')));
  assert(guide.includes(pulses.toLocaleString()+' pulses/rev'));
  for(const axis of ['x','y']){
    const dropdown=html.match(new RegExp('<select id="microsteps_'+axis+'">([\\s\\S]*?)</select>'));
    assert(dropdown, 'Pulse setting dropdown is missing');
    assert(dropdown[1].includes('value="'+microsteps+'"'));
  }
}
controller.motion('x',800,32,120);
for(const unsupported of [1,256,512]){
  elements.get('microsteps_x').value=String(unsupported);
  controller.rescaleMicrostepCalibration('x');
  assert.equal(Number(elements.get('steps_x').value),800);
}
console.log('Canvas dimensions, rotation, physical coordinates, and Fit to canvas verified.');
console.log('Pulse ceiling and physical microstep calibration scaling verified.');
