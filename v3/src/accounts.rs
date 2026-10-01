use crate::store::Store;
use anyhow::{anyhow, Result};
use serde_json::{json, Value};
use sqlx::Row;

const ADMIN_ID: &str = "admin";

impl Store {
    pub async fn init_accounts(&self) -> Result<()> {
        sqlx::raw_sql(
            "CREATE TABLE IF NOT EXISTS user_accounts(
                user_id TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                value BLOB NOT NULL
            );
            CREATE TABLE IF NOT EXISTS user_profiles(
                user_id TEXT PRIMARY KEY REFERENCES user_accounts(user_id),
                value BLOB NOT NULL
            );",
        )
        .execute(&self.app_pool)
        .await?;
        // Keep the original v3 admin document as rollback material. New requests
        // use the account and profile rows after this idempotent import.
        if let Some(legacy) = self.get("admin").await? {
            if legacy["generation"].as_str().is_some() {
                self.import_admin(&legacy).await?;
            }
        }
        Ok(())
    }

    async fn import_admin(&self, legacy: &Value) -> Result<()> {
        let username = legacy["username"]
            .as_str()
            .ok_or_else(|| anyhow!("Legacy admin has no username"))?;
        let account = json!({
            "user_id": ADMIN_ID,
            "username": username,
            "password_hash": legacy["password_hash"],
            "generation": legacy["generation"],
            "is_approved": true
        });
        let profile = json!({
            "user_id": ADMIN_ID,
            "display_name": username,
            "is_admin": true,
            "default_agent_id": "shared_default",
            "last_login_at": null
        });
        let mut tx = self.app_pool.begin().await?;
        sqlx::query("INSERT OR IGNORE INTO user_accounts(user_id,username,value) VALUES(?,?,?)")
            .bind(ADMIN_ID)
            .bind(username)
            .bind(self.seal(&account)?)
            .execute(&mut *tx)
            .await?;
        sqlx::query("INSERT OR IGNORE INTO user_profiles(user_id,value) VALUES(?,?)")
            .bind(ADMIN_ID)
            .bind(self.seal(&profile)?)
            .execute(&mut *tx)
            .await?;
        tx.commit().await?;
        Ok(())
    }

    pub async fn has_admin_account(&self) -> Result<bool> {
        let found: Option<i64> = sqlx::query_scalar("SELECT 1 FROM user_accounts WHERE user_id=?")
            .bind(ADMIN_ID)
            .fetch_optional(&self.app_pool)
            .await?;
        Ok(found.is_some())
    }

    pub async fn account_by_id(&self, id: &str) -> Result<Option<Value>> {
        let row = sqlx::query("SELECT value FROM user_accounts WHERE user_id=?")
            .bind(id)
            .fetch_optional(&self.app_pool)
            .await?;
        row.map(|r| self.unseal(&r.get::<Vec<u8>, _>("value")))
            .transpose()
    }

    pub async fn account_by_username(&self, username: &str) -> Result<Option<Value>> {
        let row = sqlx::query("SELECT value FROM user_accounts WHERE username=?")
            .bind(username)
            .fetch_optional(&self.app_pool)
            .await?;
        row.map(|r| self.unseal(&r.get::<Vec<u8>, _>("value")))
            .transpose()
    }

    pub async fn user_profile(&self, id: &str) -> Result<Option<Value>> {
        let row = sqlx::query("SELECT value FROM user_profiles WHERE user_id=?")
            .bind(id)
            .fetch_optional(&self.app_pool)
            .await?;
        row.map(|r| self.unseal(&r.get::<Vec<u8>, _>("value")))
            .transpose()
    }

    pub async fn create_admin_account(
        &self,
        username: &str,
        password_hash: &str,
        generation: &str,
    ) -> Result<()> {
        let account = json!({
            "user_id": ADMIN_ID,
            "username": username,
            "password_hash": password_hash,
            "generation": generation,
            "is_approved": true
        });
        let profile = json!({
            "user_id": ADMIN_ID,
            "display_name": username,
            "is_admin": true,
            "default_agent_id": "shared_default",
            "last_login_at": null
        });
        let legacy = json!({
            "username": username,
            "password_hash": password_hash,
            "generation": generation
        });
        let mut tx = self.app_pool.begin().await?;
        let existing: Option<i64> =
            sqlx::query_scalar("SELECT 1 FROM user_accounts WHERE user_id=?")
                .bind(ADMIN_ID)
                .fetch_optional(&mut *tx)
                .await?;
        if existing.is_some() {
            return Err(anyhow!("Admin account already exists"));
        }
        sqlx::query("INSERT INTO user_accounts(user_id,username,value) VALUES(?,?,?)")
            .bind(ADMIN_ID)
            .bind(username)
            .bind(self.seal(&account)?)
            .execute(&mut *tx)
            .await?;
        sqlx::query("INSERT INTO user_profiles(user_id,value) VALUES(?,?)")
            .bind(ADMIN_ID)
            .bind(self.seal(&profile)?)
            .execute(&mut *tx)
            .await?;
        sqlx::query("INSERT INTO docs(key,value) VALUES('admin',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value")
            .bind(self.seal(&legacy)?)
            .execute(&mut *tx)
            .await?;
        tx.commit().await?;
        Ok(())
    }

    pub async fn record_user_login(&self, id: &str, timestamp: u64) -> Result<()> {
        let mut tx = self.app_pool.begin().await?;
        let row = sqlx::query("SELECT value FROM user_profiles WHERE user_id=?")
            .bind(id)
            .fetch_one(&mut *tx)
            .await?;
        let mut profile = self.unseal(&row.get::<Vec<u8>, _>("value"))?;
        profile["last_login_at"] = json!(timestamp);
        sqlx::query("UPDATE user_profiles SET value=? WHERE user_id=?")
            .bind(self.seal(&profile)?)
            .bind(id)
            .execute(&mut *tx)
            .await?;
        tx.commit().await?;
        Ok(())
    }

    pub async fn update_admin_profile(&self, username: &str, display_name: &str) -> Result<Value> {
        let mut tx = self.app_pool.begin().await?;
        let row = sqlx::query("SELECT value FROM user_accounts WHERE user_id=?")
            .bind(ADMIN_ID)
            .fetch_one(&mut *tx)
            .await?;
        let mut account = self.unseal(&row.get::<Vec<u8>, _>("value"))?;
        account["username"] = json!(username);
        sqlx::query("UPDATE user_accounts SET username=?,value=? WHERE user_id=?")
            .bind(username)
            .bind(self.seal(&account)?)
            .bind(ADMIN_ID)
            .execute(&mut *tx)
            .await?;
        let row = sqlx::query("SELECT value FROM user_profiles WHERE user_id=?")
            .bind(ADMIN_ID)
            .fetch_one(&mut *tx)
            .await?;
        let mut profile = self.unseal(&row.get::<Vec<u8>, _>("value"))?;
        profile["display_name"] = json!(display_name);
        sqlx::query("UPDATE user_profiles SET value=? WHERE user_id=?")
            .bind(self.seal(&profile)?)
            .bind(ADMIN_ID)
            .execute(&mut *tx)
            .await?;
        let legacy = json!({"username":username,"password_hash":account["password_hash"],
            "generation":account["generation"]});
        sqlx::query("INSERT INTO docs(key,value) VALUES('admin',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value")
            .bind(self.seal(&legacy)?)
            .execute(&mut *tx)
            .await?;
        tx.commit().await?;
        Ok(json!({"user":username,"user_id":ADMIN_ID,"display_name":display_name,"is_admin":true}))
    }

    pub async fn session_days(&self, id: &str) -> Result<u64> {
        let profile = self.user_profile(id).await?;
        Ok(profile
            .as_ref()
            .and_then(|v| v["session_days"].as_u64())
            .unwrap_or(1))
    }

    pub async fn set_session_days(&self, id: &str, days: u64) -> Result<()> {
        let mut tx = self.app_pool.begin().await?;
        let row = sqlx::query("SELECT value FROM user_profiles WHERE user_id=?")
            .bind(id)
            .fetch_one(&mut *tx)
            .await?;
        let mut profile = self.unseal(&row.get::<Vec<u8>, _>("value"))?;
        profile["session_days"] = json!(days);
        sqlx::query("UPDATE user_profiles SET value=? WHERE user_id=?")
            .bind(self.seal(&profile)?)
            .bind(id)
            .execute(&mut *tx)
            .await?;
        tx.commit().await?;
        Ok(())
    }

    pub async fn auth_sessions(
        &self,
        user_id: &str,
        generation: &str,
        current_time: u64,
    ) -> Result<Vec<Value>> {
        let rows = sqlx::query("SELECT key,value FROM docs WHERE key LIKE 'auth:%' ORDER BY key")
            .fetch_all(&self.app_pool)
            .await?;
        let mut sessions = Vec::new();
        for row in rows {
            let key: String = row.get("key");
            let value = self.unseal(&row.get::<Vec<u8>, _>("value"))?;
            if value["expires"].as_u64().unwrap_or(0) <= current_time
                || value["generation"] != generation
                || value["user_id"].as_str().unwrap_or("admin") != user_id
            {
                continue;
            }
            sessions.push(json!({"id":value["id"].as_str().unwrap_or(&key[5..]),
                "key":key,"user_agent":value["user_agent"],"created_at":value["created_at"],
                "expires_at":value["expires"]}));
        }
        Ok(sessions)
    }

    pub async fn replace_admin_password(
        &self,
        expected_generation: &str,
        password_hash: &str,
        generation: &str,
    ) -> Result<()> {
        let mut tx = self.app_pool.begin().await?;
        let row = sqlx::query("SELECT value FROM user_accounts WHERE user_id=?")
            .bind(ADMIN_ID)
            .fetch_one(&mut *tx)
            .await?;
        let mut account = self.unseal(&row.get::<Vec<u8>, _>("value"))?;
        if account["generation"] != expected_generation {
            return Err(anyhow!("Account changed; sign in again"));
        }
        account["password_hash"] = json!(password_hash);
        account["generation"] = json!(generation);
        sqlx::query("UPDATE user_accounts SET value=? WHERE user_id=?")
            .bind(self.seal(&account)?)
            .bind(ADMIN_ID)
            .execute(&mut *tx)
            .await?;
        let legacy = json!({
            "username": account["username"],
            "password_hash": password_hash,
            "generation": generation
        });
        sqlx::query("INSERT INTO docs(key,value) VALUES('admin',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value")
            .bind(self.seal(&legacy)?)
            .execute(&mut *tx)
            .await?;
        tx.commit().await?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn imports_existing_v3_admin_and_profile_without_changing_password_hash() {
        let root = tempfile::tempdir().unwrap();
        let store = Store::open(root.path()).await.unwrap();
        store
            .put(
                "admin",
                &json!({"username":"operator","password_hash":"hash",
            "generation":"generation"}),
            )
            .await
            .unwrap();
        store.init_accounts().await.unwrap();
        store.init_accounts().await.unwrap();
        let account = store
            .account_by_username("operator")
            .await
            .unwrap()
            .unwrap();
        assert_eq!(account["user_id"], ADMIN_ID);
        assert_eq!(account["password_hash"], "hash");
        assert_eq!(
            store.user_profile(ADMIN_ID).await.unwrap().unwrap()["is_admin"],
            true
        );
        assert!(store.has_admin_account().await.unwrap());
    }

    #[tokio::test]
    async fn first_admin_claim_creates_account_profile_and_rollback_document() {
        let root = tempfile::tempdir().unwrap();
        let store = Store::open(root.path()).await.unwrap();
        store
            .create_admin_account("operator", "hash", "generation")
            .await
            .unwrap();
        assert!(store
            .create_admin_account("second", "hash", "other")
            .await
            .is_err());
        assert!(store.account_by_username("second").await.unwrap().is_none());
        assert_eq!(
            store.account_by_id(ADMIN_ID).await.unwrap().unwrap()["username"],
            "operator"
        );
        assert_eq!(
            store.user_profile(ADMIN_ID).await.unwrap().unwrap()["default_agent_id"],
            "shared_default"
        );
        assert_eq!(
            store.get("admin").await.unwrap().unwrap()["username"],
            "operator"
        );
    }
}
