/**
 * Unit test for the decision layer, run outside n8n.
 *
 * Extracts the `Parse AI Result` and `Apply Business Rules` Code nodes straight
 * out of workflows/wf_main_triage.json, feeds them each case from
 * evaluation/test_cases.json with a stubbed well-behaved model, and asserts the
 * queue / priority / approval / rule expectations.
 *
 * This is the fast feedback loop: it catches a broken rule in a second, without
 * Docker, without an OpenAI key, and without the network. The full harness in
 * evaluation/run_eval.py still tests the deployed pipeline end to end.
 *
 * Usage:  node tools/test_rules.js
 * Exit 0 when every rules-layer expectation holds.
 */

'use strict';

const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const wf = JSON.parse(fs.readFileSync(path.join(ROOT, 'workflows/wf_main_triage.json'), 'utf8'));
const spec = JSON.parse(fs.readFileSync(path.join(ROOT, 'evaluation/test_cases.json'), 'utf8'));

const codeOf = (name) => {
  const n = wf.nodes.find((x) => x.name === name);
  if (!n) throw new Error(`node not found: ${name}`);
  return n.parameters.jsCode;
};

const JS_PARSE = codeOf('Parse AI Result');
const JS_RULES = codeOf('Apply Business Rules');

// --- directory stub, mirroring services/itsm-api/main.py -------------------
const EMPLOYEES = {
  'arun.mehta@tcs-demo.local':     { department: 'Banking & Financial Services', is_vip: false },
  'priya.nair@tcs-demo.local':     { department: 'Banking & Financial Services', is_vip: false },
  'raghav.iyer@tcs-demo.local':    { department: 'Enterprise IT',                is_vip: true  },
  'sneha.rao@tcs-demo.local':      { department: 'Retail & CPG',                 is_vip: false },
  'vikram.singh@tcs-demo.local':   { department: 'Life Sciences',                is_vip: false },
  'meera.krishnan@tcs-demo.local': { department: 'Enterprise IT',                is_vip: true  },
};

const KB_AUTO = /password|reset|locked|expired/i;

// Mirrors the INJECTION_PATTERNS list in the `Validate Request` node.
const INJECTION_PATTERNS = [
  /ignore\s+(all\s+)?(previous|prior|above)\s+instructions/i,
  /disregard\s+(the\s+)?(system|previous)\s+prompt/i,
  /you\s+are\s+now\s+(a|an)\s+/i,
  /reveal\s+(your\s+)?(system\s+)?prompt/i,
  /\bBEGIN\s+SYSTEM\b/i,
];

// --- n8n global stubs -----------------------------------------------------
const $env = { APPROVAL_COST_THRESHOLD: '25000', MIN_LLM_CONFIDENCE: '0.55' };
const $execution = { id: 'test' };
const $workflow = { name: 'wf_main_triage' };
const $now = { toISO: () => new Date().toISOString() };

let CTX = null;
let INPUT = null;
const $input = { first: () => INPUT, all: () => [INPUT] };
const $ = () => ({ first: () => ({ json: CTX }), item: { json: CTX } });

const run = (js) => {
  const fn = new Function('$input', '$', '$env', '$execution', '$workflow', '$now', js);
  return fn($input, $, $env, $execution, $workflow, $now)[0].json;
};

const parseAI = (ctx, aiResponse) => { CTX = ctx; INPUT = { json: aiResponse }; return run(JS_PARSE); };
const applyRules = (parsed) => { INPUT = { json: parsed }; return run(JS_RULES); };

/**
 * Stands in for a *well-behaved* model. The point of this test is the rules
 * layer, so the stub classifies competently and we assert that the rules do the
 * right thing on top of a reasonable classification.
 */
function stubModel(p) {
  const t = `${p.subject || ''} ${p.description || ''}`.toLowerCase();

  const sec = /phish|malware|ransom|breach|compromis|unauthori|\.locked/.test(t);
  const cat =
      sec ? 'security'
    : /vpn|wi-?fi|network|dns|firewall|subnet|shared network drive/.test(t) ? 'network'
    : /access|permission|role|group|admin rights|production database|distribution group|project folder|password/.test(t) ? 'access'
    : /laptop|monitor|keyboard|dock|battery|screen|printer/.test(t) ? 'hardware'
    : /install|software|licence|license|outlook|excel|teams|conferencing|camera|reporting application|tableau|ide|data centre/.test(t) ? 'software'
    : 'other';

  const vague = /not working properly|thing that was fixed|same as before/.test(t);
  const pri =
      sec ? 'P1'
    : /entire team|whole team|all users|production down|business stopped/.test(t) ? 'P1'
    : /how do i|i want to know/.test(t) ? 'P4'
    : 'P3';

  return {
    model: 'stub-model',
    usage: { prompt_tokens: 10, completion_tokens: 10 },
    choices: [{ message: { content: JSON.stringify({
      category: cat,
      subcategory: 'test',
      priority: pri,
      urgency: 'medium',
      sentiment: /unacceptable|third time|nobody has called/.test(t) ? 'frustrated' : 'neutral',
      is_security_incident: sec,
      suggested_queue: 'L1_SERVICE_DESK',
      summary: 'stubbed summary',
      entities: {},
      confidence: vague ? 0.35 : 0.85,
    }) } }],
  };
}

function contextFor(p) {
  const emp = EMPLOYEES[(p.requester_email || '').toLowerCase()];
  const text = `${p.subject || ''} ${p.description || ''}`;
  return {
    ticket_id: 'INC-TEST', trace_id: 'TR-TEST', started_at_ms: Date.now() - 100,
    channel: 'webhook',
    requester_email: p.requester_email || '',
    requester_name: p.requester_name || '',
    subject: p.subject || '',
    description: p.description || '',
    requested_category: (p.category || '').toLowerCase(),
    requested_cost: Number(p.requested_cost || 0),
    requested_priority: '',
    affected_asset_tag: p.affected_asset_tag || '',
    department: emp ? emp.department : 'Unknown',
    is_vip: emp ? emp.is_vip : false,
    directory_verified: Boolean(emp),
    manager_email: 'mgr@tcs-demo.local',
    assets_summary: '', asset_count: 0,
    kb_best_match_id: KB_AUTO.test(text) ? 'KB0001' : null,
    kb_best_match_title: null, kb_resolution: null,
    kb_auto_resolvable: KB_AUTO.test(text),
    kb_score: 0.5,
    enrichment_ok: true,
    injection_suspected: INJECTION_PATTERNS.some((re) => re.test(text)),
  };
}

// --- run ------------------------------------------------------------------
let pass = 0, fail = 0, skipped = 0;
const failures = [];

console.log('');
console.log('  Decision-layer test  (Parse AI Result -> Apply Business Rules)');
console.log('  ' + '-'.repeat(74));

for (const c of spec.cases) {
  const exp = c.expect || {};

  // Validation-only cases never reach the rules layer.
  if (exp.http_status === 400) { skipped++; continue; }

  const out = applyRules(parseAI(contextFor(c.payload), stubModel(c.payload)));

  const errs = [];
  if (exp.queue && out.queue !== exp.queue) errs.push(`queue: got ${out.queue}, want ${exp.queue}`);
  if (exp.priority && out.priority !== exp.priority) errs.push(`priority: got ${out.priority}, want ${exp.priority}`);
  if (exp.priority_in && !exp.priority_in.includes(out.priority)) errs.push(`priority: got ${out.priority}, want one of ${exp.priority_in}`);
  if (exp.requires_approval !== undefined && out.requires_approval !== exp.requires_approval)
    errs.push(`requires_approval: got ${out.requires_approval}, want ${exp.requires_approval}`);
  for (const r of exp.rules || []) {
    if (!out.rules_applied.includes(r)) errs.push(`rule ${r} did not fire (fired: ${out.rules_applied.join(',') || 'none'})`);
  }

  if (errs.length) {
    fail++;
    failures.push({ id: c.case_id, errs, out });
  } else {
    pass++;
    console.log(`  PASS  ${c.case_id.padEnd(7)} ${String(out.queue).padEnd(16)} ${out.priority}  `
      + `approval=${String(out.requires_approval).padEnd(5)}  ${out.rules_applied.join(',')}`);
  }
}

if (failures.length) {
  console.log('');
  console.log('  FAILURES');
  console.log('  ' + '-'.repeat(74));
  for (const f of failures) {
    console.log(`  FAIL  ${f.id}  queue=${f.out.queue} priority=${f.out.priority} `
      + `approval=${f.out.requires_approval} rules=${f.out.rules_applied.join(',')}`);
    for (const e of f.errs) console.log(`          -> ${e}`);
  }
}

console.log('');
console.log(`  ${pass} passed, ${fail} failed, ${skipped} skipped (validation-only cases)`);
console.log('');

process.exit(fail ? 1 : 0);
