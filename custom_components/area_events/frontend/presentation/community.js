import { element, PRESENTATION_CONTRACT } from "./contract.js";

const el = element;
export const styles = `
  .community-event { min-height: 100%; }
  .community-event .image {
    position: relative; aspect-ratio: 16 / 9; overflow: hidden;
    display: flex; align-items: center; justify-content: center;
    background-color: color-mix(in srgb, var(--primary-text-color, currentColor) 10%, transparent);
    color: var(--secondary-text-color);
  }
  .community-event .image img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; opacity: 0; transition: opacity 0.25s; }
  .community-event .image img.loaded { opacity: 1; }
  .community-event .art { position: absolute; inset: 0; --ae-accent: hsl(210 60% 55%); --ae-bg: color-mix(in srgb, var(--ae-accent) 16%, var(--card-background-color, Canvas)); }
  .community-event .art svg { width: 100%; height: 100%; display: block; }
  .community-event .art .bg, .community-event .art .e { fill: var(--ae-bg); }
  .community-event .art .a { fill: var(--ae-accent); }
  .community-event .art .b { fill: color-mix(in srgb, var(--ae-accent) 38%, transparent); }
  .community-event .art .c { fill: color-mix(in srgb, var(--primary-text-color, CanvasText) 62%, transparent); }
  .community-event .art .s, .community-event .art .sb, .community-event .art .sc, .community-event .art .se { fill: none; stroke-width: 4; stroke-linecap: round; stroke-linejoin: round; }
  .community-event .art .s { stroke: var(--ae-accent); }
  .community-event .art .sb { stroke: color-mix(in srgb, var(--ae-accent) 45%, transparent); }
  .community-event .art .sc { stroke: color-mix(in srgb, var(--primary-text-color, CanvasText) 55%, transparent); stroke-width: 3; }
  .community-event .art .se { stroke: var(--ae-bg); stroke-width: 5; }
  .community-event .art .p1 { fill: hsl(8 78% 60%); } .community-event .art .p2 { fill: hsl(46 92% 58%); } .community-event .art .p3 { fill: hsl(205 75% 56%); } .community-event .art .p4 { fill: hsl(140 52% 48%); }
  .community-event .art.music { --ae-accent: hsl(285 58% 58%); } .community-event .art.theater { --ae-accent: hsl(345 62% 56%); } .community-event .art.festival { --ae-accent: hsl(28 86% 54%); }
  .community-event .art.food { --ae-accent: hsl(12 74% 56%); } .community-event .art.sports { --ae-accent: hsl(150 52% 42%); } .community-event .art.arts { --ae-accent: hsl(262 52% 60%); }
  .community-event .art.science { --ae-accent: hsl(195 70% 46%); } .community-event .art.family { --ae-accent: hsl(42 88% 50%); } .community-event .art.community { --ae-accent: hsl(172 52% 42%); }
  .community-event .art.outdoors { --ae-accent: hsl(128 46% 42%); } .community-event .art.general { --ae-accent: hsl(220 52% 58%); }
  .community-event .art.farmers_market { --ae-accent: hsl(100 46% 42%); } .community-event .art.carnival { --ae-accent: hsl(340 70% 55%); } .community-event .art.car_show { --ae-accent: hsl(358 68% 50%); }
  .community-event .art.halloween { --ae-accent: hsl(26 90% 52%); } .community-event .art.christmas { --ae-accent: hsl(145 48% 38%); } .community-event .art.street_festival { --ae-accent: hsl(318 58% 54%); }
  .community-event .pin { position: absolute; left: 50%; top: 50%; width: 30px; height: 40px; transform: translate(-50%, -100%); pointer-events: none; filter: drop-shadow(0 2px 2px rgba(0, 0, 0, 0.45)); }
  .community-event .pin svg { width: 100%; height: 100%; display: block; }
  .community-event .venue-chip {
    position: absolute; padding: 1px 8px; border-radius: 12px; font-size: 0.6875rem; line-height: 1.5; box-sizing: border-box; max-width: calc(100% - 16px);
    background: rgba(37, 40, 45, 0.86); color: rgb(232 234 238); overflow: hidden; white-space: nowrap; text-overflow: ellipsis;
  }
  .community-event .credit-button {
    position: absolute; left: 8px; bottom: 8px; width: 22px; height: 22px; padding: 0; margin: 0; border-radius: 50%; box-sizing: border-box; cursor: pointer;
    display: flex; align-items: center; justify-content: center; font: inherit;
    background: rgba(37, 40, 45, 0.72); color: rgb(232 234 238); border: 1px solid rgba(255, 255, 255, 0.35);
  }
  .community-event .credit-button::after { content: ""; position: absolute; inset: -11px; }
  .community-event .credit-button:hover, .community-event .credit-button[aria-expanded="true"] { background: rgba(37, 40, 45, 0.94); }
  .community-event .credit-button:focus-visible, .community-event .media-credit a:focus-visible { outline: 2px solid rgb(240 138 36); outline-offset: 2px; }
  .community-event .credit-button svg { width: 14px; height: 14px; display: block; }
  .community-event .media-credit {
    position: absolute; left: 8px; bottom: 38px; max-width: calc(100% - 16px); max-height: calc(100% - 52px); overflow: auto; box-sizing: border-box;
    padding: 6px 10px; border-radius: 8px; font-size: 0.75rem; line-height: 1.45; background: rgba(37, 40, 45, 0.96); color: rgb(232 234 238);
  }
  .community-event .media-credit a { color: inherit; text-decoration: underline; }
  .community-event .media-credit .note { display: block; opacity: 0.8; }
  .community-event .venue-chip { right: 8px; top: 8px; max-width: calc(100% - 90px); font-weight: 600; }
  .community-event .art-tag.media-badge { background: rgba(37, 40, 45, 0.88); color: rgb(240 138 36); border-color: rgba(255, 255, 255, 0.22); font-weight: 700; letter-spacing: 0.04em; }
  .community-event .art-tag {
    position: absolute; right: 8px; bottom: 8px; padding: 1px 8px; border-radius: 12px; font-size: 0.75rem; box-sizing: border-box;
    background: var(--card-background-color, var(--primary-background-color)); border: 1px solid var(--divider-color); color: var(--secondary-text-color);
  }
  .community-event .category {
    position: absolute; top: 8px; left: 8px; max-width: calc(100% - 16px);
    padding: 2px 8px; border-radius: 12px; box-sizing: border-box;
    background: var(--card-background-color, var(--primary-background-color));
    border: 1px solid var(--divider-color);
    color: var(--primary-text-color);
    font-size: 0.8125rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .community-event .verify {
    display: flex; align-items: flex-start; gap: 6px; line-height: 1.4;
    margin-top: 4px; font-size: 0.8125rem;
    font-weight: min(var(--ha-font-weight-medium, 500), 600);
  }
  .community-event .verify.verified { color: var(--success-color, var(--primary-text-color)); }
  .community-event .verify.corroborated { color: var(--secondary-text-color); }
  .community-event .verify.unverified { color: var(--secondary-text-color); }
  .community-event .verify ha-icon { --mdc-icon-size: 16px; flex: none; width: 16px; height: 1.4em; display: inline-flex; align-items: center; justify-content: center; }
  .community-event .verify > span { min-width: 0; }
  .community-event .description {
    font-size: 0.875rem; color: var(--secondary-text-color); overflow-wrap: anywhere;
    display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden;
  }
  .community-event .source { margin-top: 4px; font-size: 0.8125rem; color: var(--primary-color); overflow-wrap: anywhere; }
  @media (prefers-reduced-motion: reduce) { .community-event .image img { transition: none; } }
`;
/*
 * Category artwork. Original flat illustrations drawn for this card (no third-party assets), embedded here so they
 * ship with the card, need no network and follow the Home Assistant theme: every colour is a theme variable or a
 * tint of one. They are shown when an event has no usable photo, and are always labelled as illustrations.
 * Which one an event gets is decided by the engine from what the event is about (discovery/classify.py) and arrives
 * as event.illustration.category; the card only draws it, and events without one get "general".
 */
const SVG_HEAD = '<svg viewBox="0 0 320 180" preserveAspectRatio="xMidYMid slice" aria-hidden="true" focusable="false"><rect class="bg" width="320" height="180"/>';
export const ART = {
  music: SVG_HEAD +
    '<g class="b"><rect x="28" y="96" width="10" height="40" rx="5"/><rect x="48" y="78" width="10" height="76" rx="5"/><rect x="68" y="62" width="10" height="108" rx="5"/>' +
    '<rect x="242" y="62" width="10" height="108" rx="5"/><rect x="262" y="78" width="10" height="76" rx="5"/><rect x="282" y="96" width="10" height="40" rx="5"/></g>' +
    '<g class="a"><ellipse cx="136" cy="128" rx="20" ry="15" transform="rotate(-20 136 128)"/><ellipse cx="196" cy="116" rx="20" ry="15" transform="rotate(-20 196 116)"/>' +
    '<rect x="150" y="42" width="8" height="82"/><rect x="210" y="30" width="8" height="82"/><path d="M150 42 L218 30 L218 54 L150 66 Z"/></g></svg>',
  theater: SVG_HEAD +
    '<path class="b" d="M0 0 H70 Q60 90 40 180 H0 Z"/><path class="b" d="M320 0 H250 Q260 90 280 180 H320 Z"/>' +
    '<ellipse class="a" cx="130" cy="92" rx="42" ry="52"/><ellipse class="e" cx="116" cy="80" rx="8" ry="6"/><ellipse class="e" cx="146" cy="80" rx="8" ry="6"/>' +
    '<path class="se" d="M108 108 Q130 134 152 108"/>' +
    '<ellipse class="c" cx="200" cy="104" rx="38" ry="48"/><ellipse class="e" cx="187" cy="94" rx="7" ry="5"/><ellipse class="e" cx="213" cy="94" rx="7" ry="5"/>' +
    '<path class="se" d="M184 128 Q200 112 216 128"/></svg>',
  festival: SVG_HEAD +
    '<path class="sc" d="M0 30 Q160 70 320 30"/>' +
    '<path class="a" d="M28 39 L52 39 L40 65 Z"/><path class="c" d="M68 45 L92 45 L80 71 Z"/><path class="a" d="M108 49 L132 49 L120 75 Z"/>' +
    '<path class="c" d="M148 50 L172 50 L160 76 Z"/><path class="a" d="M188 49 L212 49 L200 75 Z"/><path class="c" d="M228 45 L252 45 L240 71 Z"/><path class="a" d="M268 39 L292 39 L280 65 Z"/>' +
    '<g class="s"><path d="M110 92 V108 M110 148 V164 M72 128 H88 M132 128 H148 M83 101 L94 112 M126 144 L137 155 M137 101 L126 112 M94 144 L83 155"/></g><circle class="a" cx="110" cy="128" r="6"/>' +
    '<g class="sb"><path d="M236 104 V116 M236 144 V156 M208 130 H220 M252 130 H264 M216 110 L224 118 M248 142 L256 150 M256 110 L248 118 M224 142 L216 150"/></g><circle class="b" cx="236" cy="130" r="5"/></svg>',
  food: SVG_HEAD +
    '<circle class="a" cx="160" cy="96" r="54"/><circle class="e" cx="160" cy="96" r="40"/><circle class="b" cx="160" cy="96" r="22"/>' +
    '<g class="c"><rect x="76" y="42" width="5" height="34" rx="2.5"/><rect x="86" y="42" width="5" height="34" rx="2.5"/><rect x="96" y="42" width="5" height="34" rx="2.5"/>' +
    '<rect x="76" y="70" width="25" height="10" rx="5"/><rect x="85" y="78" width="7" height="68" rx="3.5"/>' +
    '<path d="M240 42 Q264 58 258 104 H240 Z"/><rect x="240" y="102" width="18" height="44" rx="5"/></g></svg>',
  sports: SVG_HEAD +
    '<path class="b" d="M0 152 H320 V180 H0 Z"/><g class="s"><path d="M52 66 H98 M40 90 H90 M54 114 H100"/></g>' +
    '<circle class="a" cx="170" cy="88" r="48"/><path class="e" d="M170 64 L192 80 L184 106 L156 106 L148 80 Z"/>' +
    '<g class="se"><path d="M170 64 V42 M192 80 L214 72 M184 106 L196 126 M156 106 L144 126 M148 80 L126 72"/></g>' +
    '<ellipse class="c" cx="170" cy="150" rx="40" ry="6"/></svg>',
  arts: SVG_HEAD +
    '<path class="a" d="M160 34 C100 34 60 70 60 108 C60 142 92 152 112 148 C128 145 124 128 140 128 C156 128 160 142 176 144 C236 152 264 120 260 92 C256 56 214 34 160 34 Z"/>' +
    '<circle class="p1" cx="106" cy="92" r="11"/><circle class="p2" cx="144" cy="68" r="11"/><circle class="p3" cx="192" cy="70" r="11"/><circle class="p4" cx="226" cy="98" r="11"/>' +
    '<ellipse class="e" cx="136" cy="116" rx="9" ry="7"/>' +
    '<g transform="rotate(32 270 110)"><rect class="c" x="266" y="40" width="9" height="86" rx="4.5"/><rect class="a" x="263" y="124" width="15" height="22" rx="4"/></g></svg>',
  science: SVG_HEAD +
    '<circle class="b" cx="44" cy="40" r="3"/><circle class="b" cx="284" cy="56" r="4"/><circle class="b" cx="270" cy="150" r="3"/><circle class="b" cx="52" cy="140" r="4"/>' +
    '<g class="s"><ellipse cx="160" cy="90" rx="78" ry="28"/><ellipse cx="160" cy="90" rx="78" ry="28" transform="rotate(60 160 90)"/><ellipse cx="160" cy="90" rx="78" ry="28" transform="rotate(120 160 90)"/></g>' +
    '<circle class="a" cx="160" cy="90" r="13"/><circle class="c" cx="238" cy="90" r="6"/><circle class="c" cx="199" cy="158" r="6"/><circle class="c" cx="121" cy="158" r="6"/></svg>',
  family: SVG_HEAD +
    '<circle class="b" cx="262" cy="46" r="28"/><path class="b" d="M0 150 Q80 112 160 140 T320 132 V180 H0 Z"/>' +
    '<circle class="a" cx="112" cy="70" r="15"/><rect class="a" x="96" y="88" width="32" height="58" rx="15"/>' +
    '<circle class="a" cx="208" cy="70" r="15"/><rect class="a" x="192" y="88" width="32" height="58" rx="15"/>' +
    '<circle class="c" cx="160" cy="100" r="11"/><rect class="c" x="148" y="114" width="24" height="32" rx="11"/>' +
    '<path class="s" d="M128 112 L148 124 M192 112 L172 124"/></svg>',
  community: SVG_HEAD +
    '<path class="b" d="M160 30 C152 14 130 22 138 38 L160 58 L182 38 C190 22 168 14 160 30 Z"/>' +
    '<circle class="a" cx="106" cy="86" r="17"/><path class="a" d="M74 156 Q106 104 138 156 Z"/>' +
    '<circle class="a" cx="214" cy="86" r="17"/><path class="a" d="M182 156 Q214 104 246 156 Z"/>' +
    '<circle class="c" cx="160" cy="76" r="19"/><path class="c" d="M124 160 Q160 98 196 160 Z"/></svg>',
  outdoors: SVG_HEAD +
    '<circle class="b" cx="244" cy="46" r="24"/><path class="b" d="M0 150 L90 58 L150 122 L212 68 L320 150 V180 H0 Z"/>' +
    '<path class="a" d="M0 180 V152 L60 112 L110 152 L172 122 L232 162 L282 132 L320 160 V180 Z"/>' +
    '<path class="c" d="M236 166 L254 120 L272 166 Z"/><path class="c" d="M262 166 L276 130 L290 166 Z"/></svg>',
  farmers_market: SVG_HEAD +
    '<path class="b" d="M0 150 Q80 120 160 140 T320 132 V180 H0 Z"/>' +
    '<path class="a" d="M60 62 H260 L276 100 H44 Z"/><g class="e"><path d="M92 62 L84 100 H116 L120 62 Z"/><path d="M152 62 L152 100 H184 L188 62 Z"/><path d="M220 62 L224 100 H252 L244 62 Z"/></g>' +
    '<path class="c" d="M48 100 H272 V112 H48 Z"/><rect class="c" x="54" y="112" width="7" height="46" rx="3"/><rect class="c" x="259" y="112" width="7" height="46" rx="3"/>' +
    '<rect class="a" x="72" y="126" width="176" height="14" rx="4"/>' +
    '<circle class="p1" cx="96" cy="120" r="9"/><circle class="p2" cx="118" cy="121" r="9"/><circle class="p4" cx="142" cy="120" r="9"/>' +
    '<circle class="p3" cx="168" cy="121" r="8"/><circle class="p1" cx="192" cy="120" r="9"/><circle class="p2" cx="216" cy="121" r="9"/></svg>',
  carnival: SVG_HEAD +
    '<circle class="b" cx="48" cy="40" r="3"/><circle class="b" cx="276" cy="36" r="4"/><circle class="b" cx="30" cy="96" r="3"/>' +
    '<circle class="s" cx="160" cy="92" r="62"/><circle class="a" cx="160" cy="92" r="9"/>' +
    '<g class="sb"><path d="M160 30 V154 M98 92 H222 M116 48 L204 136 M204 48 L116 136"/></g>' +
    '<circle class="p1" cx="160" cy="30" r="8"/><circle class="p2" cx="204" cy="48" r="8"/><circle class="p3" cx="222" cy="92" r="8"/><circle class="p4" cx="204" cy="136" r="8"/>' +
    '<circle class="p1" cx="160" cy="154" r="8"/><circle class="p2" cx="116" cy="136" r="8"/><circle class="p3" cx="98" cy="92" r="8"/><circle class="p4" cx="116" cy="48" r="8"/>' +
    '<path class="c" d="M160 98 L120 170 H134 L160 122 L186 170 H200 Z"/><path class="b" d="M0 168 H320 V180 H0 Z"/></svg>',
  car_show: SVG_HEAD +
    '<path class="b" d="M0 142 H320 V180 H0 Z"/><g class="sb"><path d="M20 160 H70 M120 160 H160 M220 160 H280"/></g>' +
    '<path class="a" d="M44 130 L52 108 Q56 100 66 98 L108 94 L132 70 Q138 64 148 64 H186 Q196 64 202 70 L226 94 L262 100 Q276 102 278 116 V130 Q278 136 272 136 H50 Q44 136 44 130 Z"/>' +
    '<path class="e" d="M122 94 L142 74 H188 L208 94 Z"/><path class="se" d="M165 76 V94"/>' +
    '<circle class="c" cx="96" cy="136" r="19"/><circle class="e" cx="96" cy="136" r="8"/><circle class="c" cx="226" cy="136" r="19"/><circle class="e" cx="226" cy="136" r="8"/>' +
    '<circle class="a" cx="268" cy="114" r="5"/><path class="c" d="M268 40 L274 54 L290 56 L278 66 L282 82 L268 74 L254 82 L258 66 L246 56 L262 54 Z"/></svg>',
  halloween: SVG_HEAD +
    '<circle class="b" cx="250" cy="48" r="26"/><circle class="e" cx="260" cy="42" r="22"/>' +
    '<path class="c" d="M30 64 q8 -10 16 0 q8 -10 16 0 q-4 12 -16 14 q-12 -2 -16 -14 Z"/><path class="c" d="M268 112 q6 -8 12 0 q6 -8 12 0 q-3 9 -12 10 q-9 -1 -12 -10 Z"/>' +
    '<path class="b" d="M0 152 Q80 132 160 148 T320 140 V180 H0 Z"/>' +
    '<ellipse class="a" cx="160" cy="108" rx="62" ry="50"/><path class="b" d="M160 58 Q150 48 160 40 Q170 48 160 58 Z"/><rect class="c" x="154" y="40" width="12" height="16" rx="4"/>' +
    '<path class="e" d="M120 94 L142 94 L131 74 Z"/><path class="e" d="M178 94 L200 94 L189 74 Z"/><path class="e" d="M144 110 L160 122 L176 110 Z"/>' +
    '<path class="e" d="M116 126 L128 134 L140 126 L152 136 L164 126 L176 136 L188 126 L200 134 L204 126 L204 142 H116 Z"/></svg>',
  christmas: SVG_HEAD +
    '<circle class="b" cx="44" cy="38" r="3"/><circle class="b" cx="90" cy="22" r="2.5"/><circle class="b" cx="262" cy="30" r="3"/><circle class="b" cx="292" cy="64" r="2.5"/><circle class="b" cx="60" cy="86" r="2.5"/><circle class="b" cx="250" cy="96" r="3"/>' +
    '<path class="b" d="M0 150 Q80 134 160 148 T320 140 V180 H0 Z"/>' +
    '<path class="a" d="M160 26 L198 78 H178 L212 120 H186 L226 160 H94 L134 120 H108 L142 78 H122 Z"/>' +
    '<rect class="c" x="148" y="160" width="24" height="16" rx="3"/>' +
    '<polygon class="p2" points="160,10 164,21 176,21 166,28 170,40 160,33 150,40 154,28 144,21 156,21"/>' +
    '<circle class="p1" cx="140" cy="106" r="7"/><circle class="p3" cx="176" cy="84" r="7"/><circle class="p2" cx="182" cy="134" r="7"/><circle class="p1" cx="122" cy="146" r="7"/><circle class="p3" cx="158" cy="60" r="6"/></svg>',
  street_festival: SVG_HEAD +
    '<path class="sc" d="M0 26 Q80 62 160 36 T320 30"/>' +
    '<path class="p1" d="M30 44 L52 46 L38 68 Z"/><path class="p2" d="M76 54 L98 54 L88 76 Z"/><path class="p3" d="M122 52 L144 48 L136 72 Z"/><path class="p4" d="M178 46 L200 50 L186 70 Z"/><path class="p1" d="M224 50 L246 54 L232 74 Z"/><path class="p2" d="M270 44 L292 40 L284 64 Z"/>' +
    '<path class="b" d="M0 150 H320 V180 H0 Z"/>' +
    '<path class="a" d="M36 148 V104 Q36 84 56 84 H92 Q112 84 112 104 V148 Z"/><path class="e" d="M48 148 V108 H100 V148 Z"/>' +
    '<path class="c" d="M120 148 V96 Q120 76 140 76 H180 Q200 76 200 96 V148 Z"/><path class="e" d="M132 148 V100 H188 V148 Z"/>' +
    '<path class="a" d="M208 148 V108 Q208 88 228 88 H264 Q284 88 284 108 V148 Z"/><path class="e" d="M220 148 V112 H272 V148 Z"/>' +
    '<circle class="p2" cx="74" cy="124" r="9"/><circle class="p1" cx="160" cy="122" r="10"/><circle class="p4" cx="246" cy="130" r="9"/></svg>',
  general: SVG_HEAD +
    '<circle class="b" cx="52" cy="46" r="3"/><circle class="b" cx="268" cy="44" r="4"/><circle class="b" cx="262" cy="146" r="3"/><circle class="b" cx="60" cy="138" r="4"/>' +
    '<rect class="a" x="96" y="46" width="128" height="104" rx="14"/><rect class="e" x="106" y="72" width="108" height="68" rx="8"/>' +
    '<rect class="c" x="124" y="34" width="10" height="26" rx="5"/><rect class="c" x="186" y="34" width="10" height="26" rx="5"/>' +
    '<polygon class="a" points="160,84 165.3,98.7 180.9,99.2 168.6,108.8 172.9,123.8 160,115 147.1,123.8 151.4,108.8 139.1,99.2 154.7,98.7"/></svg>',
};
export const ART_LABELS = {
  music: "Music and concerts", theater: "Theater and performing arts", festival: "Festivals", food: "Food and drink", sports: "Sports",
  arts: "Arts and exhibitions", science: "Science and technology", family: "Family activities", community: "Community events",
  outdoors: "Outdoors and recreation", general: "Events",
  farmers_market: "Farmers markets", carnival: "Carnivals and fairs", car_show: "Car shows", halloween: "Halloween",
  christmas: "Christmas and holiday events", street_festival: "Street festivals",
};
/** The illustration for an event: the category the engine resolved (see discovery/classify.py), else the general one. */
export function artFor(event) {
  const resolved = event && event.illustration && event.illustration.category;
  return typeof resolved === "string" && Object.prototype.hasOwnProperty.call(ART, resolved) ? resolved : "general";
}

/*
 * Location imagery from the Area Events app (event.media): a verified photograph of the venue and/or a map. Everything is
 * validated here as data from outside: addresses must be this integration's own /area_events/media/ files, links must be
 * https addresses on the expected sites, and every text is shown as text. The card draws the pin, badges, venue name and
 * credits itself; the map files are vector images in a light and a graphite variant with the same geography.
 */

const PIN_SVG = '<svg viewBox="0 0 30 40" aria-hidden="true" focusable="false"><path d="M15 39 L5 23 A13 13 0 1 1 25 23 Z" fill="#f08a24" stroke="#fff" stroke-width="2.4" stroke-linejoin="round"/>' +
  '<circle cx="15" cy="14" r="5.2" fill="#25282d"/></svg>';
const MEDIA_BADGES = { venue: "VENUE", map: "MAP" };
const INFO_SVG = '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><circle cx="12" cy="12" r="10" fill="none" stroke="currentColor" stroke-width="2"/>' +
  '<path d="M12 11v6" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/><circle cx="12" cy="7.2" r="1.5" fill="currentColor"/></svg>';
// The credit a map must carry (OpenFreeMap's required text, the OpenStreetMap copyright page) lives behind a small info button on the tile.
const MAP_CREDITS = [["OpenFreeMap", "https://openfreemap.org"], [" © OpenMapTiles", "https://openmaptiles.org/"], [" Data from OpenStreetMap", "https://www.openstreetmap.org/copyright"]];
function renderCommunityEvent(event, context) {
    const item = el("div", "event community-event");

    const image = el("div", "image");
    const artKind = artFor(event);
    // The illustration is always there first, so a tile never shows an empty box, a broken-image icon or a layout shift.
    const art = el("div", `art ${artKind}`);
    art.innerHTML = ART[artKind];
    art.setAttribute("role", "img");
    art.setAttribute("aria-label", `Illustration: ${ART_LABELS[artKind]}`);
    const tag = el("span", "art-tag", "Illustration");
    image.append(art, tag);
    // Image priority: the event's own photos (the engine put its best first, then verified ones from other matched records), a
    // verified photograph of the venue, a map of the event's location, and finally the category illustration, which is always
    // underneath. An image that fails to load hands over to the next one. Only the event's own photos are ever shown without
    // a label: VENUE and MAP say what the rest are.
    const media = event.media;
    const place = media && media.map && media.map.venue;
    const candidates = [];
    if (context.showImages) {
      for (const url of event.images) candidates.push({ kind: "event", url: () => url, alt: `Image for ${event.title || "this event"}`, credit: event.imageCredit });
      if (media && media.photo) candidates.push({ kind: "venue", url: () => media.photo.url, alt: `Photograph of the venue${media.photo.venue ? `, ${media.photo.venue}` : ""}, not of the event`, photo: media.photo });
      if (media && media.map) {
        candidates.push({ kind: "map", url: () => (context.isDark() ? media.map.dark : media.map.light), map: media.map,
          alt: `Map of the event location${place ? `: ${place}` : ""}${media.map.approximate ? " (approximate position)" : ""}` });
      }
    }
    if (candidates.length) {
      let current = candidates.shift();
      const img = document.createElement("img");
      let requested; // the address last given to the image, and the one that last loaded (img.src itself is the resolved absolute address)
      let loaded;
      const show = (url) => { requested = url; img.src = url; };
      const pin = el("span", "pin");
      pin.innerHTML = PIN_SVG;
      pin.hidden = true;
      const credit = el("div", "media-credit");
      credit.hidden = true;
      context.nextCreditId();
      credit.id = `ae-credit-${context.nextCreditId()}`;
      // a small information button; the full credit and its links open from it and close again with it, Escape or a change of image
      const info = el("button", "credit-button");
      info.type = "button";
      info.hidden = true;
      info.innerHTML = INFO_SVG;
      info.setAttribute("aria-expanded", "false");
      info.setAttribute("aria-controls", credit.id);
      const collapse = (focus) => {
        credit.hidden = true;
        info.setAttribute("aria-expanded", "false");
        if (focus && info.focus) info.focus();
      };
      info.addEventListener("click", () => {
        credit.hidden = !credit.hidden;
        info.setAttribute("aria-expanded", String(!credit.hidden));
      });
      const escape = (e) => { if (e && e.key === "Escape" && !credit.hidden) collapse(true); };
      info.addEventListener("keydown", escape);
      credit.addEventListener("keydown", escape);
      const chip = el("span", "venue-chip");
      chip.hidden = true;
      const fill = () => {
        // what the image that is showing needs around it: a pin and the map credit, or the photograph's credit
        credit.replaceChildren();
        collapse(false);
        info.hidden = current.kind !== "map" && current.kind !== "venue" && !(current.kind === "event" && current.credit);
        info.setAttribute("aria-label", current.kind === "map" ? "Map credits and licence" : "Photo credit and licence");
        info.title = info.getAttribute("aria-label");
        pin.hidden = current.kind !== "map";
        chip.hidden = current.kind !== "map" || !place;
        if (!chip.hidden) chip.textContent = place;   // the position's confidence stays in the data and the credit note; the card says once, at the top, that areas are matched by source
        if (current.kind === "venue") {
          const { creator, license, licenseUrl, sourceUrl } = current.photo;
          credit.append(document.createTextNode(`Photo: ${creator || "Wikimedia Commons"} · `));
          const licence = el(licenseUrl ? "a" : "span", "", license || "see file page");
          if (licenseUrl) Object.assign(licence, { href: licenseUrl, target: "_blank", rel: "noopener noreferrer" });
          credit.append(licence);
          if (sourceUrl) {
            const source = el("a", "", " · Wikimedia Commons");
            Object.assign(source, { href: sourceUrl, target: "_blank", rel: "noopener noreferrer" });
            credit.append(source);
          }
        } else if (current.kind === "event" && current.credit) {
          credit.append(document.createTextNode(`Photo: ${current.credit.text}`));
          if (current.credit.license) credit.append(el("span", "note", current.credit.license));
          if (current.credit.sourceUrl) {
            const source = el("a", "", "Event page");
            Object.assign(source, { href: current.credit.sourceUrl, target: "_blank", rel: "noopener noreferrer" });
            credit.append(document.createTextNode(" · "), source);
          }
        } else if (current.kind === "map") {
          credit.append(document.createTextNode("Map: "));
          for (const [label, href] of MAP_CREDITS) {
            const source = el("a", "", label);
            Object.assign(source, { href, target: "_blank", rel: "noopener noreferrer" });
            credit.append(source);
          }
          if (current.map.approximate) credit.append(el("span", "note", "The pin shows an approximate position."));
        }
      };
      img.alt = current.alt;
      img.loading = "lazy";
      img.decoding = "async";
      img.referrerPolicy = "no-referrer";
      img.addEventListener("load", () => {
        loaded = requested;
        img.className = "loaded";
        art.hidden = true;
        const badge = MEDIA_BADGES[current.kind];
        tag.textContent = badge || "Illustration";
        tag.className = badge ? "art-tag media-badge" : "art-tag";
        tag.hidden = !badge;
        fill();
      });
      img.addEventListener("error", () => {
        // back to the illustration (and its label) while the next image loads, or for good when none is left
        img.className = "";
        art.hidden = false;
        tag.textContent = "Illustration";
        tag.className = "art-tag";
        tag.hidden = false;
        pin.hidden = chip.hidden = info.hidden = true;
        collapse(false);
        if (current.kind === "map" && loaded && requested !== loaded) {
          // the other theme's variant failed after this map had loaded: the map is still good, so keep showing the one that loaded
          // (the next theme change tries again) instead of giving up on the map
          show(loaded);
          return;
        }
        loaded = undefined;
        current = candidates.shift();
        if (current) {
          img.alt = current.alt;
          show(current.url());
        } else {
          img.remove();
          item._aeFailed = Date.now(); // every image failed: later polls may try again (see _retryImages)
        }
      });
      // a theme change swaps the map being shown for its other variant (only the one in use is ever requested)
      item._aeTheme = () => {
        if (current && current.kind === "map" && requested !== current.url()) show(current.url());
      };
      show(current.url());
      image.append(img, pin, chip, info, credit);
    }
    if (event.category) image.append(el("span", "category", event.category));

    const body = el("div", "body");
    body.append(el("div", "title", event.title || "Untitled event"));
    if (event.start) body.append(context.meta("mdi:calendar-clock", context.formatDate(event.start, event.timezone)));
    if (event.location) body.append(context.meta("mdi:map-marker", event.location));
    if (event.locationStatus === "description_only" || event.locationStatus === "unknown") {
      body.append(context.meta("mdi:help-circle-outline", event.location ? "The location is the source's own description and is not verified." : "The source gave no location."));
    }
    if (event.description) body.append(el("div", "description", event.description));

    // Only an explicit verified=true is "Verified"; a corroborated status means
    // the source's own signals agree, which is not independent verification.
    let kind = "unverified";
    let label = "Unverified";
    let icon = "mdi:help-circle-outline";
    if (event.verified) [kind, label, icon] = ["verified", "Verified", "mdi:check-decagram"];
    else if (event.status === "corroborated") [kind, label, icon] = ["corroborated", "Source corroborated", "mdi:check-circle-outline"];
    const verify = el("div", `verify ${kind}`);
    verify.append(context.icon(icon), el("span", "", label));
    if (event.evidence.length) verify.title = event.evidence.join("\n");
    body.append(verify);

    if (/^https?:\/\//i.test(event.url || "")) {
      const link = el("a", "source", `Source: ${event.sourceName || "event page"}`);
      link.href = event.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      body.append(link);
    }

    item.append(image, body);
    return item;
}

export const communityRenderer = {
  id: "community", contract: PRESENTATION_CONTRACT, eventTypes: ["community.event"], render: renderCommunityEvent, styles,
  // All listeners are attached to the returned tile and are released with that DOM subtree.
  cleanup() {},
};
