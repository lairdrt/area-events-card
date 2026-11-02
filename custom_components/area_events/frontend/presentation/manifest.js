import { PresentationRegistry } from "./registry.js?v=7ac27f0958aa";
import { communityRenderer } from "./community.js?v=5181097f1cb8";
import { genericRenderer } from "./generic.js?v=aa3f635bd928";
import { nasaMissionRenderer } from "./nasa-mission.js?v=6308bbbd10a7";

/** Renderer registration manifest. Adding a renderer changes this manifest, not host selection logic. */
export const PRESENTATIONS = new PresentationRegistry([communityRenderer, genericRenderer, nasaMissionRenderer]);
