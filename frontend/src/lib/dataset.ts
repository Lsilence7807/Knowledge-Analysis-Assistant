// 文件：frontend/src/lib/dataset.ts
// 作用：当前选中数据集的共享状态：提问、数据集页、设置页都要读，存 localStorage 供刷新后接着用
// 阶段：F2a 前端工程
// 依赖：react（useSyncExternalStore）
import { useSyncExternalStore } from "react";

const STORAGE_KEY = "kaa.dataset_id";
const listeners = new Set<() => void>();
let current = localStorage.getItem(STORAGE_KEY) ?? "";

export function setDatasetId(id: string): void {
  current = id;
  localStorage.setItem(STORAGE_KEY, id);
  for (const notify of listeners) notify();
}

/** 给传输层用：发请求那一刻取，不必跟着 React 渲染走。 */
export function getDatasetId(): string {
  return current;
}

export function useDatasetId(): string {
  return useSyncExternalStore(
    (notify) => {
      listeners.add(notify);
      return () => {
        listeners.delete(notify);
      };
    },
    () => current,
  );
}
