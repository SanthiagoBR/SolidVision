import { Link } from "react-router";

import styles from "./NotFound.module.css";

/** A path of the interface that names no screen: the API answers every one
 * of them with the index (RFC-037 section 5.1), and this says so. */
export function NotFound() {
  return (
    <div className={styles.notFound}>
      <h1 className={styles.title}>Esta tela não existe</h1>
      <Link className={styles.link} to="/">
        Voltar ao início
      </Link>
    </div>
  );
}
