import { createContext, useContext } from "react";
import type { Schema } from "./api/client";

export const CapitalContext = createContext<Schema["Capital"]>("450000");
export const useCapital = () => useContext(CapitalContext);
