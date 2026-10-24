import { PresentationRegistry } from "./registry.js";
import { communityRenderer } from "./community.js";
import { genericRenderer } from "./generic.js";
import { nasaMissionRenderer } from "./nasa-mission.js";

/** Renderer registration manifest. Adding a renderer changes this manifest, not host selection logic. */
export const PRESENTATIONS = new PresentationRegistry([communityRenderer, genericRenderer, nasaMissionRenderer]);
