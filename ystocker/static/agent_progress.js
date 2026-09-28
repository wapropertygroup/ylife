/**
 * agent_progress.js — how far a TradingAgents run has got, from the events it
 * has published.
 *
 * A run is a fixed sequence of turns (agents.run_plan, which the job poll
 * carries as `plan`): each analyst in roster order, bull and bear alternating
 * for 2 × debate_rounds turns, the Research Manager, the Trader, aggressive,
 * conservative and neutral in turn for 3 × risk_rounds, and the Portfolio
 * Manager. The child publishes one event per finished turn, so counting events
 * per role says exactly which turns are done. This is a count, not a clock:
 * turns take anything from twenty seconds to several minutes, and a bar driven
 * by elapsed time would be a guess drawn as a measurement.
 *
 * Two rules keep a missed event from stalling the bar. Everything before a
 * turn that has reported counts as done, because the graph only moves forward
 * -- an analyst that published nothing (an empty report is never written) must
 * not hold the Analysts phase at 5/6 while the debate goes on. And every count
 * is clamped to its phase, so a role that re-publishes cannot push past it.
 *
 * Pure: no DOM, no fetch, no clock. agents.html draws the result;
 * tests/check_agent_progress.mjs pins the arithmetic.
 */
(function (root) {
  'use strict';

  var RISK = ['aggressive', 'conservative', 'neutral'];

  function rounds(raw) {
    var n = parseInt(raw, 10);
    return n > 0 ? n : 1;
  }

  /**
   * `plan`  — {analysts: [role keys in order], debate_rounds, risk_rounds}
   * `turns` — {role key: events seen for it}
   *
   * Returns {done, total, phases, current}. `phases` is the six stages in
   * order, each {key, done, total, state: 'done' | 'active' | 'todo'};
   * `current` is the turn under way, {role, round, rounds} (round and rounds
   * only in the two debates), or null once every turn has reported and the
   * report is being put together.
   */
  function compute(plan, turns) {
    plan = plan || {};
    turns = turns || {};
    var count = function (role) {
      var n = Number(turns[role]);
      return n > 0 ? Math.floor(n) : 0;
    };
    var analysts = (plan.analysts || []).slice();
    var debateRounds = rounds(plan.debate_rounds);
    var riskRounds = rounds(plan.risk_rounds);

    // The analysts run one after another, so the last one to report marks
    // every one before it done as well.
    var lastAnalyst = -1;
    analysts.forEach(function (role, i) { if (count(role) > 0) lastAnalyst = i; });

    var phases = [
      { key: 'analysts', done: lastAnalyst + 1, total: analysts.length },
      { key: 'debate', done: count('bull') + count('bear'), total: 2 * debateRounds },
      { key: 'research_mgr', done: count('research_mgr'), total: 1 },
      { key: 'trader', done: count('trader'), total: 1 },
      { key: 'risk', done: RISK.reduce(function (s, r) { return s + count(r); }, 0),
        total: 3 * riskRounds },
      { key: 'portfolio', done: count('portfolio'), total: 1 },
    ].filter(function (p) { return p.total > 0; });

    phases.forEach(function (p) { p.done = Math.min(p.done, p.total); });

    // A phase with any progress closes every phase before it.
    var reached = -1;
    phases.forEach(function (p, i) { if (p.done > 0) reached = i; });
    phases.forEach(function (p, i) { if (i < reached) p.done = p.total; });

    var active = -1;
    for (var i = 0; i < phases.length; i++) {
      if (phases[i].done < phases[i].total) { active = i; break; }
    }
    phases.forEach(function (p, i) {
      p.state = p.done >= p.total ? 'done' : (i === active ? 'active' : 'todo');
    });

    var current = null;
    if (active >= 0) {
      var p = phases[active];
      if (p.key === 'analysts') {
        current = { role: analysts[p.done] };
      } else if (p.key === 'debate') {
        // Bull opens, then the two alternate: an even count means bull is up.
        current = { role: p.done % 2 === 0 ? 'bull' : 'bear',
                    round: Math.floor(p.done / 2) + 1, rounds: debateRounds };
      } else if (p.key === 'risk') {
        current = { role: RISK[p.done % 3],
                    round: Math.floor(p.done / 3) + 1, rounds: riskRounds };
      } else {
        current = { role: p.key };
      }
    }

    var done = 0, total = 0;
    phases.forEach(function (p) { done += p.done; total += p.total; });
    return { done: done, total: total, phases: phases, current: current };
  }

  var api = { compute: compute };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.AgentProgress = api;
})(typeof window !== 'undefined' ? window : this);
