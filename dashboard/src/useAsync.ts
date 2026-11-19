import { useCallback, useEffect, useState, type DependencyList } from "react";

export type AsyncState<T> = { status: "loading" } | { status: "error"; error: unknown } | { status: "ready"; data: T };

/** Runs an async loader when deps change, ignoring results from superseded runs. */
export function useAsync<T>(loader: () => Promise<T>, deps: DependencyList): AsyncState<T> & { reload: () => void } {
  const [state, setState] = useState<AsyncState<T>>({ status: "loading" });
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let current = true;
    setState({ status: "loading" });
    loader().then(
      (data) => current && setState({ status: "ready", data }),
      (error: unknown) => current && setState({ status: "error", error }),
    );
    return () => {
      current = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { ...state, reload };
}
