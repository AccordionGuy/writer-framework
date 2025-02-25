<!-- eslint-disable @typescript-eslint/ban-types -->
<template>
	<Teleport to="#modal">
		<div class="BuilderModal" tabindex="-1" @keydown="handleKeydown">
			<div class="main">
				<div class="titleContainer">
					<i v-if="icon" class="material-symbols-outlined">{{
						icon
					}}</i>
					<h2>{{ modalTitle }}</h2>
					<button
						:title="closeAction?.desc ?? 'Close'"
						@click="closeAction.fn"
					>
						<i class="material-symbols-outlined">close</i>
					</button>
				</div>
				<div class="slotContainer">
					<slot></slot>
				</div>
				<div v-if="menuActions?.length > 0" class="actionContainer">
					<button
						v-for="(action, index) in menuActions"
						:key="index"
						@click="action.fn"
					>
						{{ action.desc }}
					</button>
				</div>
			</div>
		</div>
	</Teleport>
</template>

<script setup lang="ts">
import { toRefs } from "vue";

export type ModalAction = {
	desc: string;
	fn: (..._args: unknown[]) => unknown;
};

const props = defineProps<{
	modalTitle: string;
	icon?: string;
	closeAction: ModalAction;
	menuActions?: ModalAction[];
}>();

const { modalTitle, icon, closeAction, menuActions } = toRefs(props);

const handleKeydown = (ev: KeyboardEvent) => {
	if (ev.key == "Escape") {
		closeAction.value.fn();
	}
};
</script>

<style scoped>
@import "./sharedStyles.css";

.BuilderModal {
	background: rgba(0, 0, 0, 0.1);
	width: 100vw;
	height: 100vh;
	display: flex;
	align-items: center;
	justify-content: center;
}

.main {
	background: white;
	width: 80%;
	overflow: hidden;
	max-width: 120ch;
	border-radius: 8px;
	box-shadow: 0 0 16px 4px rgba(0, 0, 0, 0.3);
}

.titleContainer {
	border-bottom: 1px solid var(--builderSubtleSeparatorColor);
	padding: 10px 10px 10px 16px;
	gap: 16px;
	display: flex;
	align-items: center;
	font-size: 1rem;
}

.titleContainer button {
	margin-left: auto;
}

.slotContainer {
	padding: 16px;
	max-height: 60vh;
	overflow: auto;
}

.actionContainer {
	border-top: 1px solid var(--builderSubtleSeparatorColor);
	padding: 16px;
}
</style>
