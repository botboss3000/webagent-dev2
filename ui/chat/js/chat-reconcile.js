import {app} from '../../shared/js/state.js';
import {refreshPanel,disconnectPanel} from '../core/panel.js';

// The public subscription owns replay and reconnection. There is no private
// interaction-tail renderer or polling authority in the customer transcript.
function startReconcileLoop() {}
function stopReconcileLoop() {disconnectPanel();}
function seedReconcileSig() {}
function shouldPollIdleTail() {return false;}
app.startReconcileLoop=startReconcileLoop;
app.stopReconcileLoop=stopReconcileLoop;
app.seedReconcileSig=seedReconcileSig;
export {startReconcileLoop,stopReconcileLoop,seedReconcileSig,shouldPollIdleTail};
