import { useDeleteDeleteFlows } from "@/controllers/API/queries/flows/use-delete-delete-flows";
import useFlowsManagerStore from "@/stores/flowsManagerStore";
import { useTypesStore } from "@/stores/typesStore";
import {
  extractFieldsFromComponenents,
  processFlows,
} from "@/utils/reactflowUtils";

const useDeleteFlow = () => {
  const flows = useFlowsManagerStore((state) => state.flows);
  const setFlows = useFlowsManagerStore((state) => state.setFlows);
  const setIsLoading = useFlowsManagerStore((state) => state.setIsLoading);

  const { mutate } = useDeleteDeleteFlows();

  const deleteFlow = async ({
    id,
  }: {
    id: string | string[];
  }): Promise<void> => {
    return new Promise<void>((resolve, reject) => {
      if (!Array.isArray(id)) {
        id = [id];
      }
      mutate(
        { flow_ids: id },
        {
          onSuccess: () => {
            const { data, flows: myFlows } = processFlows(
              flows.filter((flow) => !id.includes(flow.id)),
            );
            setFlows(myFlows);
            setIsLoading(false);
            useTypesStore.setState((state) => ({
              data: { ...state.data, ["saved_components"]: data },
              ComponentFields: extractFieldsFromComponenents({
                ...state.data,
                ["saved_components"]: data,
              }),
            }));

            resolve();
          },
          onError: (e) => reject(e),
        },
      );
    });
  };

  return deleteFlow;
};

export default useDeleteFlow;
