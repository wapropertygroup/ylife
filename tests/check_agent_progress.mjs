/**
 * Behaviour tests for static/agent_progress.js — the arithmetic behind the
 * progress bar on /agents while a run is working.
 *
 * The failures worth catching all render as a plausible bar: a turn counted
 * twice pushes past 100% or skips a speaker, a missed event stalls the bar
 * while the run goes on, and a debate that alternates the wrong way names the
 * wrong researcher as "now speaking". So these walk a whole run event by event
 * and check the label at every step, not just the percentage.
 *
 * Run: node tests/check_agent_progress.mjs
 */
import { createRequire } from 'module';
import path from 'path';

const root = path.resolve(import.meta.dirname, '..');
const require = createRequire(import.meta.url);
const { compute } = require(path.join(root, 'ystocker/static/agent_progress.js'));

const failures = [];
const t = (label, cond, detail = '') => {
  console.log(`  ${cond ? 'ok  ' : 'FAIL'} ${label}${detail ? '  ' + detail : ''}`);
  if (!cond) failures.push(label);
};

const US = { analysts: ['market', 'sentiment', 'news', 'earnings', 'quality', 'valuation'],
             debate_rounds: 3, risk_rounds: 3 };
const ASHARE = { analysts: [...US.analysts, 'policy', 'hot_money', 'lockup'],
                 debate_rounds: 3, risk_rounds: 3 };

// The order the graph runs in, one role per finished turn (TradingAgents'
// conditional_logic: bull opens, bear answers; aggressive, conservative, neutral).
function sequence(plan) {
  const seq = [...plan.analysts];
  for (let r = 0; r < plan.debate_rounds; r++) seq.push('bull', 'bear');
  seq.push('research_mgr', 'trader');
  for (let r = 0; r < plan.risk_rounds; r++) seq.push('aggressive', 'conservative', 'neutral');
  seq.push('portfolio');
  return seq;
}

const tally = roles => roles.reduce((m, r) => ({ ...m, [r]: (m[r] || 0) + 1 }), {});

console.log('\nshape');
{
  const p = compute(US, {});
  t('a US run is 24 turns (6 + 2x3 + 1 + 1 + 3x3 + 1)', p.total === 24, `got ${p.total}`);
  t('nothing done before the first event', p.done === 0);
  t('the first analyst is up before anything reports', p.current && p.current.role === 'market');
  t('six phases, in order', p.phases.map(x => x.key).join() ===
    'analysts,debate,research_mgr,trader,risk,portfolio');
  t('the first phase is active, the rest to do',
    p.phases.map(x => x.state).join() === 'active,todo,todo,todo,todo,todo');
  t('an A-share run is 27 turns', compute(ASHARE, {}).total === 27);
  t('rounds come from the plan', compute({ ...US, debate_rounds: 1, risk_rounds: 2 }, {}).total === 6 + 2 + 2 + 6 + 1);
}

console.log('\na whole run, one event at a time');
{
  const seq = sequence(US);
  let ok = true, detail = '';
  for (let i = 0; i < seq.length; i++) {
    const p = compute(US, tally(seq.slice(0, i)));
    const want = seq[i];
    if (p.done !== i || !p.current || p.current.role !== want) {
      ok = false; detail = `at ${i}: done=${p.done} current=${p.current && p.current.role}, want ${want}`;
      break;
    }
  }
  t('after k events, k turns are done and turn k is the one named', ok, detail);
  const end = compute(US, tally(seq));
  t('every turn reported: all done, nobody named', end.done === 24 && end.current === null);
  t('every phase done at the end', end.phases.every(x => x.state === 'done'));
}

console.log('\nthe debates say which round');
{
  const upTo = n => tally(sequence(US).slice(0, n));
  const a = compute(US, upTo(6));              // analysts done, bull opens
  t('round 1 opens with the bull', a.current.role === 'bull' && a.current.round === 1 && a.current.rounds === 3);
  const b = compute(US, upTo(7));
  t('then the bear answers in round 1', b.current.role === 'bear' && b.current.round === 1);
  const c = compute(US, upTo(8));
  t('round 2 opens with the bull again', c.current.role === 'bull' && c.current.round === 2);
  const d = compute(US, upTo(6 + 6 + 2));      // into the risk debate
  t('the risk debate opens with the aggressive analyst, round 1',
    d.current.role === 'aggressive' && d.current.round === 1);
  const e = compute(US, upTo(6 + 6 + 2 + 4));
  t('its fourth turn is round 2, conservative', e.current.role === 'conservative' && e.current.round === 2);
  t('the manager and the trader carry no round', compute(US, upTo(12)).current.round === undefined);
}

console.log('\na missed event does not stall the bar');
{
  // `quality` published nothing -- an empty report is never written -- but
  // the valuation analyst after it did, and so did the bull.
  const turns = tally(['market', 'sentiment', 'news', 'earnings', 'valuation', 'bull']);
  const p = compute(US, turns);
  t('the analysts phase is closed once the debate has started', p.phases[0].state === 'done');
  t('and the count follows the graph, not the events', p.done === 7, `got ${p.done}`);
  t('the bear is up', p.current.role === 'bear');
}

console.log('\nthe analysts finish in any order');
{
  // TradingAgents 0.6 runs them at the same time. This is the order a real
  // DeepSeek run on the box published them in, 2026-10-08.
  const box = ['quality', 'sentiment', 'valuation', 'earnings', 'market', 'news'];
  const first = compute(US, tally(box.slice(0, 1)));
  t('one in, quality first: one done, not five', first.done === 1, `got ${first.done}`);
  t('and the first one still out is named', first.current.role === 'market');
  const three = compute(US, tally(box.slice(0, 3)));
  t('valuation in, four still out: the debate has not started',
    three.phases[0].state === 'active' && three.phases[1].state === 'todo' && three.done === 3,
    `done=${three.done} states=${three.phases.map(x => x.state)}`);
  const five = compute(US, tally(box.slice(0, 5)));
  t('five in: news is the one named', five.done === 5 && five.current.role === 'news');
  const all = compute(US, tally(box));
  t('all six in: the bull opens round 1', all.done === 6 && all.current.role === 'bull' && all.current.round === 1);
  const q = compute(US, tally(['market', 'news']));
  t('market and news in: two done, not three', q.done === 2, `got ${q.done}`);
  t('and sentiment, the first still out, is named', q.current.role === 'sentiment');
}

console.log('\ncounts are clamped');
{
  const p = compute(US, { ...tally(sequence(US).slice(0, 13)), trader: 5 });
  t('a role that re-publishes cannot push its phase past its size',
    p.phases[3].done === 1 && p.done === 14, `done=${p.done}`);
  const q = compute(US, tally([...sequence(US), 'bull', 'neutral', 'portfolio']));
  t('never more than the total', q.done === q.total);
  t('junk counts are ignored', compute(US, { market: 'x', bull: -3 }).done === 0);
  t('a missing plan does not throw', compute(undefined, undefined).total === 2 + 1 + 1 + 3 + 1);
}

console.log('');
if (failures.length) {
  console.log(`${failures.length} FAILED`);
  process.exit(1);
}
console.log('all passed');
