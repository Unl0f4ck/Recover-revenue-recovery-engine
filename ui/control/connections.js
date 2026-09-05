/* Secrets are submitted once over same-origin HTTP(S), never browser storage. */
'use strict';
function renderAccount(state) {
  const a=state.account;
  $('#account-status').textContent=a.connected?`${a.name} · ${a.key_hint} · TEST MODE · expires ${new Date(a.expires).toLocaleTimeString()}`:'Server workspace. Connect your own test account below.';
  $('#account-disconnect').hidden=!a.connected;
  $('#account-webhook-path').textContent=a.webhook_path||'Connect to get your callback path';
  $('#connection').textContent=a.connected?a.name+' · test mode':'Engine connected';
}
function clearAccountView() {
  source='live';data=null;activeCase='';proposalId=null;
  $('#case-dialog').close();$('#overview-data').hidden=true;$('#empty').hidden=true;
  $('#cases').innerHTML='';$('#messages-list').innerHTML='';
  $('#live-result').hidden=true;$('#csv-result').textContent='';
  $('#delivery-email').checked=$('#delivery-sms').checked=false;
  if(pollTimer){clearTimeout(pollTimer);pollTimer=null;}busy=false;
  $('#progress').hidden=true;$('#batch-submit').disabled=false;
}
$('#account-form').onsubmit=async e=>{
  e.preventDefault();const button=$('#account-submit');button.disabled=true;
  const body={name:$('#account-name').value,key_id:$('#account-key').value,key_secret:$('#account-secret').value,webhook_secret:$('#account-webhook').value};
  try {
    await json('/api/account/connect',{method:'POST',body:JSON.stringify(body)});
    clearAccountView();await refresh();notice('Test account verified. Your account workspace is active; nothing was created or sent.');
  } catch(err){notice(err.message,true);}
  finally {
    $('#account-secret').value=$('#account-webhook').value='';
    body.key_secret=body.webhook_secret='';button.disabled=false;
  }
};
$('#account-disconnect').onclick=async()=>{
  if(!confirm('Disconnect this browser and discard its credentials? Audit records will remain.'))return;
  try{await json('/api/account',{method:'DELETE'});clearAccountView();source='';await refresh();notice('Disconnected. Credentials removed; audit records retained.');}
  catch(err){notice(err.message,true);}
};
async function loadMessages(){
  if(!source){$('#messages-list').innerHTML='<tr><td colspan="5">Select a data source first.</td></tr>';return;}
  try {
    const result=await json('/api/notifications?source='+encodeURIComponent(source));
    $('#messages-note').textContent=result.note;
    $('#messages-list').innerHTML=result.messages.map(m=>`<tr><td><button class="ref-button" data-ref="${esc(m.reference)}">${esc(m.reference)}</button></td><td>${esc(m.customer_ref||'Unknown')}</td><td>${esc(m.channel.toUpperCase())}</td><td>${badge(m.status.toUpperCase())}</td><td>${esc(m.detail)}<small>${esc(new Date(m.at).toLocaleString())}</small></td></tr>`).join('')||'<tr><td colspan="5" class="empty-line">No SMS or email requests in this workspace yet.</td></tr>';
    bindRefs($('#messages-list'));
  }catch(err){notice(err.message,true);}
}
$('#messages-refresh').onclick=loadMessages;
$('#source').addEventListener('change',()=>{if(!$('#page-messages').hidden)loadMessages();});
const sendPanel=document.createElement('section');
sendPanel.className='reply-section';
sendPanel.innerHTML='<h3>Send this recovery link</h3><p>Razorpay uses the known customer contact on this case. Closed cases, opt-outs, promises, quiet hours and duplicate requests are blocked.</p><div class="reply-buttons"><button class="secondary" id="case-email">Send email</button><button class="secondary" id="case-sms">Send SMS</button></div><div id="case-messages" class="caption"></div><div id="case-send-result" class="result-box" hidden></div>';
$('#case-dialog').insertBefore(sendPanel,$('.timeline-title'));
function renderCaseMessages(detail){
  sendPanel.hidden=source!=='live';
  $('#case-send-result').hidden=true;
  $('#case-email').disabled=$('#case-sms').disabled=detail.terminal;
  $('#case-messages').textContent=(detail.notifications||[]).filter(n=>['email','sms'].includes(n.channel)).slice(-4).map(n=>`${n.channel}: ${n.status} — ${n.detail}`).join('\n')||'No notification has been requested for this case.';
}
async function sendCase(channel){
  if(source!=='live'||!activeCase)return;
  if(!confirm(`Request a real ${channel.toUpperCase()} notification for ${activeCase}? I confirm this customer agreed to be contacted. No new payment link will be created.`))return;
  $('#case-email').disabled=$('#case-sms').disabled=true;
  try {
    const result=await json(`/api/cases/${encodeURIComponent(activeCase)}/notify`,{method:'POST',body:JSON.stringify({channel,confirm_contact:true})});
    $('#case-send-result').textContent=result.status.toUpperCase()+': '+result.detail;
    if(!$('#page-messages').hidden)await loadMessages();
  }catch(err){$('#case-send-result').textContent=err.message;}
  finally{$('#case-send-result').hidden=false;$('#case-email').disabled=$('#case-sms').disabled=false;}
}
$('#case-email').onclick=()=>sendCase('email');$('#case-sms').onclick=()=>sendCase('sms');
$('.live-panel p').textContent='Preview reads your active test account without sending. Execute creates up to five test links; optional SMS and email requests use the choices in Connections and require confirmation.';
refresh();
