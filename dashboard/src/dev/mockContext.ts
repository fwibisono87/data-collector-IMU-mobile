import { createContext, useContext } from 'react';
import type { ScenarioId } from '../api/mock';

/**
 * Non-null only under `npm run dev:mock`. The DevPanel (scenario picker)
 * renders when this context exists, so production builds ship no mock UI and
 * tests can mount components without it.
 */
export interface MockContextValue {
  scenario: ScenarioId;
  setScenario: (id: ScenarioId) => void;
}

const MockContext = createContext<MockContextValue | null>(null);

export const MockProvider = MockContext.Provider;

export function useMockContext(): MockContextValue | null {
  return useContext(MockContext);
}
