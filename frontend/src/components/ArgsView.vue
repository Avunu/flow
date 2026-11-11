<script setup>
import { computed } from "vue";
import { parseArgs, rawArgs } from "@/lib/toolMeta";
import ArgValue from "./ArgValue.vue";
import CodeBlock from "./CodeBlock.vue";

/** @import { PropType } from "vue" */
/** @import { ToolArgs } from "@/lib/toolMeta" */

// Entry point for a tool call's arguments: parses once, renders by shape.
// Unparseable JSON falls back to the raw payload so it's never hidden.
const props = defineProps({
	arguments: { type: /** @type {PropType<ToolArgs>} */ ([String, Object]), default: null },
	blockKeys: { type: /** @type {PropType<Set<string>>} */ (Object), default: () => new Set() },
});

const parsed = computed(() => parseArgs(props.arguments));
const hasFields = computed(() => Object.keys(parsed.value).length > 0);
const raw = computed(() => (hasFields.value ? "" : rawArgs(props.arguments)));
</script>

<template>
	<ArgValue v-if="hasFields" :value="parsed" :block-keys="blockKeys" />
	<CodeBlock v-else-if="raw" :code="raw" />
</template>
